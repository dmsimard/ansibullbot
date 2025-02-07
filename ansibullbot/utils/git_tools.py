import logging
import os
import shutil
import tarfile
import tempfile

import requests

from ansibullbot._text_compat import to_text
from ansibullbot.utils.systemtools import run_command


class GitRepoWrapper:
    def __init__(self, cachedir, repo, commit=None, rebase=True, context=None):
        self._needs_rebase = rebase
        self.repo = repo
        self.commit = commit
        self.context = context
        self._lrev_map = {}
        self._is_git = True
        self.checkoutdir = None
        self._files = []

        # allow for null repos
        if self.repo:
            urlparts = self.repo.split('/')
            urlparts = [x for x in urlparts if x and 'http' not in x]
            path_parts = list([cachedir] + urlparts)
            self.checkoutdir = os.path.join(*path_parts)
            parent = os.path.dirname(self.checkoutdir)
            if not os.path.exists(parent):
                os.makedirs(parent)

        self.commits_by_email = None
        self.files_by_commit = {}

        if repo:
            self.update(force=True)

    def exists(self, filename):
        return filename in self.files

    @property
    def isgit(self):
        return not self.repo.endswith('.tar.gz')

    def isdir(self, filename):
        if self.context:
            checkfile = os.path.join(self.checkoutdir, self.context, filename)
        else:
            checkfile = os.path.join(self.checkoutdir, filename)
        return os.path.isdir(checkfile)

    @property
    def files(self):
        self.get_files()
        if self.context:
            _files = self._files[:]
            _files = [x for x in _files if x.startswith(self.context)]
            _files = [x.replace(self.context.rstrip('/') + '/', '') for x in _files]
            return _files
        return self._files

    @property
    def module_files(self):
        return [x for x in self._files if x.startswith('lib/ansible/modules')]

    def create_checkout(self):
        """checkout ansible"""
        # cleanup
        if os.path.isdir(self.checkoutdir):
            shutil.rmtree(self.checkoutdir)
        if self.repo.endswith('.tar.gz'):
            self._is_git = False

            tfh,tfn = tempfile.mkstemp(suffix='.tar.gz')

            rr = requests.get(self.repo, stream=True)
            with open(tfn, 'wb') as f:
                f.write(rr.raw.read())

            os.makedirs(self.checkoutdir)
            tar = tarfile.open(tfn, "r:gz")
            tar.extractall(path=self.checkoutdir)

        else:
            cmd = "git clone %s %s" \
                % (self.repo, self.checkoutdir)
            logging.debug(cmd)
            (rc, so, se) = run_command(cmd, env={'GIT_TERMINAL_PROMPT': 0, 'GIT_ASKPASS': '/bin/echo'})
            logging.debug('rc: %s' % rc)
            print(to_text(so) + to_text(se))

            if rc != 0:
                os.makedirs(self.checkoutdir)

    def update(self, force=False):
        '''Reload everything if there are new commits'''
        changed = self.manage_checkout()
        if changed or force or not self._is_git:
            self.get_files(force=True)
        self.commits_by_email = None
        self._lrev_map = {}

    def update_checkout(self):
        """rebase + pull + update the checkout"""
        changed = False

        # get a specific commit or do a rebase
        if self.commit:
            cmd = "cd %s; git log -1  | head -n1 | awk '{print $2}'" % self.checkoutdir
            (rc, so, se) = run_command(cmd)
            so = to_text(so).strip()

            if so != self.commit:
                cmd = "cd %s; git checkout %s" % (self.checkoutdir, self.commit)
                logging.debug(cmd)
                (rc, so, se) = run_command(cmd, env={'GIT_TERMINAL_PROMPT': 0, 'GIT_ASKPASS': '/bin/echo'})
                changed = True

            if rc != 0:
                self.create_checkout()
                changed = True

        else:
            changed = False

            cmd = "cd %s ; git pull --rebase" % self.checkoutdir
            logging.debug(cmd)
            (rc, so, se) = run_command(cmd, env={'GIT_TERMINAL_PROMPT': 0, 'GIT_ASKPASS': '/bin/echo'})
            so = to_text(so)
            print(so + to_text(se))

            # If rebase failed, recreate the checkout
            if rc != 0:
                self.create_checkout()
                return True
            else:
                if 'current branch devel is up to date.' not in so.lower():
                    changed = True

        self.commits_by_email = None

        return changed

    def manage_checkout(self):
        '''Check if there are any changes to the repo'''
        if not self._is_git:
            return False
        changed = False
        if not os.path.isdir(self.checkoutdir):
            self.create_checkout()
            changed = True
        elif self._needs_rebase:
            changed = self.update_checkout()
        return changed

    def get_files(self, force=False):
        '''Cache a list of filenames in the checkout'''
        if self.isgit:
            if not self._files or force:
                cmd = f'cd {self.checkoutdir}; git ls-files'
                logging.debug(cmd)
                (rc, so, se) = run_command(cmd)
                files = to_text(so).split('\n')
                files = [x.strip() for x in files if x.strip()]
                if self.context:
                    self._files = [x for x in files if self.context in files]
                self._files = files
        else:
            self._files = []
            cmd = f'cd {self.checkoutdir}; find .'
            logging.debug(cmd)
            (rc, so, se) = run_command(cmd)
            filepaths = to_text(so).split('\n')
            for fp in filepaths:
                if not fp.startswith('./'):
                    continue
                fp = fp.replace('./', '', 1)
                self._files.append(fp)

    def get_files_by_commit(self, commit):
        if commit not in self.files_by_commit:
            cmd = f'cd {self.checkoutdir}; git show --pretty="" --name-only {commit}'
            (rc, so, se) = run_command(cmd)
            filenames = [x.strip() for x in to_text(so).split('\n') if x.strip()]
            self.files_by_commit[commit] = filenames[:]
        else:
            filenames = self.files_by_commit[commit]

        return filenames

    def get_commits_by_email(self, email):
        '''Map an email(s) to a total num of commits and total by file'''
        if self.commits_by_email is None:
            commits = {}
            cmd = f'cd {self.checkoutdir}; git log --format="%h;%ae"'
            (rc, so, se) = run_command(cmd)
            lines = [x.strip() for x in to_text(so).split('\n') if x.strip()]
            for line in lines:
                parts = line.split(';')
                this_hash = parts[0]
                this_email = parts[1]
                if this_email not in commits:
                    commits[this_email] = set()
                commits[this_email].add(this_hash)
            self.commits_by_email = commits

        if not isinstance(email, (set, list)):
            emails = [email]
        else:
            emails = [x for x in email]

        email_map = {}

        for _email in emails:
            if _email not in email_map:
                email_map[_email] = {
                    'commit_count': 0,
                    'commit_count_byfile': {}
                }

            if _email in self.commits_by_email:

                email_map[_email]['commit_count'] = \
                    len(self.commits_by_email[_email])

                for _commit in self.commits_by_email[_email]:
                    filenames = self.get_files_by_commit(_commit)
                    for fn in filenames:
                        if fn not in email_map[_email]['commit_count_byfile']:
                            email_map[_email]['commit_count_byfile'][fn] = 0
                        email_map[_email]['commit_count_byfile'][fn] += 1

        return email_map

    def get_last_rev_for_file(self, filepath):
        ''' Retrive last hash for a file if it ever existed '''
        # https://stackoverflow.com/a/19727752
        # https://stackoverflow.com/a/1395463

        if filepath not in self._lrev_map:
            cmd = 'cd %s; git rev-list --max-count=1 --all -- %s' % (self.checkoutdir, filepath)
            logging.info(cmd)
            (rc, so, se) = run_command(cmd)
            lrev = so.strip().decode('utf-8')
            self._lrev_map[filepath] = lrev

        return self._lrev_map[filepath]

    def existed(self, filepath):
        '''Did a file ever exist in this repo?'''
        if self.context:
            filepath = os.path.join(self.context, filepath)

        lrev = self.get_last_rev_for_file(filepath)
        if lrev:
            return True

        return False

    def get_file_content(self, filepath, follow=False):
        fp = os.path.join(self.checkoutdir, filepath)
        if os.path.exists(fp):
            with open(fp, 'r', encoding='utf-8') as f:
                data = f.read()
            return data

        if not follow:
            return None

        lrev = self.get_last_rev_for_file(filepath)

        # https://stackoverflow.com/a/1395463
        cmd = 'cd %s; git show %s^:%s' % (self.checkoutdir, lrev, filepath)
        (rc, so, se) = run_command(cmd)
        logging.info(cmd)
        so = so.strip()
        if so.decode('utf-8').endswith('.py'):
            newpath = os.path.dirname(filepath)
            newpath = os.path.join(newpath, so.decode('utf-8'))
            lrev = self.get_last_rev_for_file(filepath)

            cmd = 'cd %s; git show %s^:%s' % (self.checkoutdir, lrev, newpath)
            logging.info(cmd)
            (rc, so, se) = run_command(cmd)
            so = so.strip()

        return so

    def find(self, pattern):
        if pattern in self._files:
            return pattern
        matches = set()
        for fn in self._files:
            if self.context and self.context not in fn:
                continue
            if fn.endswith(pattern):
                matches.add(fn)
        return matches

    def list_files_by_branch(self, branch):
        cmd = "cd %s; git ls-tree -r --name-only %s" % (self.checkoutdir, branch)
        logging.info(cmd)
        (rc, so, se) = run_command(cmd)
        res = so.strip().decode('utf-8')
        return res.splitlines()
