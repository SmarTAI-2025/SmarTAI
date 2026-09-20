import unittest
from pathlib import Path
from publish_candidate import (BRANCH, REPOSITORY, PublishError, validate_local,
                               validate_remote, existing_pr, ensure_comment)

SHA='a'*40
class FakeCommands:
    def __init__(self):
        self.values={('branch','--show-current'):BRANCH, ('status','--porcelain'):'',
          ('remote','get-url','origin'):f'https://github.com/{REPOSITORY}.git',
          ('merge-base','--is-ancestor',SHA,'HEAD'):'', ('rev-parse','HEAD'):SHA,
          ('ls-remote','origin','refs/heads/main'):SHA+'\trefs/heads/main'}
        self.diff=0;self.rows=[];self.calls=[]
    def git(self,*args):return self.values[args]
    def run(self,args,**kwargs):self.calls.append(args);return self.diff,''
    def gh_json(self,*args):
        self.calls.append(args)
        if '--method' in args: return {'html_url':'https://github.com/example/comment/1'}
        return self.rows

class Tests(unittest.TestCase):
    def setUp(self):
        self.cmd=FakeCommands();self.manifest={'repository':REPOSITORY,'branch':BRANCH,'code_sha':SHA,'base_sha':SHA}
    def test_local_ok(self):self.assertEqual(validate_local(self.cmd,self.manifest),SHA)
    def test_wrong_repo_refused(self):
        self.cmd.values[('remote','get-url','origin')]='https://github.com/someone/else.git'
        with self.assertRaises(PublishError):validate_local(self.cmd,self.manifest)
    def test_untested_runtime_refused(self):
        self.cmd.diff=1
        with self.assertRaises(PublishError):validate_local(self.cmd,self.manifest)
    def test_changed_main_refused(self):
        self.manifest['base_sha']='b'*40
        with self.assertRaises(PublishError):validate_remote(self.cmd,self.manifest)
    def test_dirty_tree_refused(self):
        self.cmd.values[('status','--porcelain')]=' M backend/config.py'
        with self.assertRaises(PublishError):validate_local(self.cmd,self.manifest)
    def test_other_base_refused(self):
        self.cmd.rows=[{'state':'OPEN','baseRefName':'other'}]
        with self.assertRaises(PublishError):existing_pr(self.cmd)
    def test_existing_comment_not_duplicated(self):
        self.cmd.rows=[{'body':'MARKER already sent','html_url':'https://github.com/existing'}]
        self.assertEqual(ensure_comment(self.cmd,61,'MARKER','text'),'https://github.com/existing')
        self.assertFalse(any('--method' in c for c in self.cmd.calls))
    def test_new_comment_created_once(self):
        ensure_comment(self.cmd,61,'MARKER','text')
        self.assertEqual(sum('--method' in c for c in self.cmd.calls),1)
if __name__=='__main__':unittest.main()
