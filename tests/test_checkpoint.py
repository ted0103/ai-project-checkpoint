import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "skills/project-checkpoint/scripts/checkpoint.py"
spec = importlib.util.spec_from_file_location("checkpoint", SCRIPT)
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir(); git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com"); git(self.root, "config", "user.name", "Test")
        (self.root / "tracked.txt").write_text("one\n"); git(self.root, "add", "."); git(self.root, "commit", "-qm", "initial")
        self.initial_branch=git(self.root,"branch","--show-current").stdout.strip().decode()
        self.draft = {
            "goal":"Ship v0.1.", "current_state":"Implementation is active.",
            "capabilities":["built — Publish and resume checkpoints — evidence: tracked.txt"],
            "decisions":["active — Use standard library — keeps installation portable."],
            "acceptance_criteria":["The unit test suite passes."],
            "verification":["Unit test observed passing."], "risks":[],
            "open_questions":["Choose the release date."], "next_action":"Run the unit tests.",
            "remainder":["Tag the release."],
        }

    def tearDown(self): self.temp.cleanup()

    def test_clean_dirty_staged_deleted_renamed_untracked_and_odd_names(self):
        clean = cp.inspect(self.root); self.assertEqual(clean["path_total"], 0)
        (self.root / "tracked.txt").write_text("two\n")
        (self.root / "stage space.txt").write_text("s"); git(self.root, "add", "stage space.txt")
        (self.root / "delete.txt").write_text("d"); git(self.root, "add", "delete.txt"); git(self.root, "commit", "-qm", "more")
        (self.root / "delete.txt").unlink(); git(self.root, "mv", "stage space.txt", "renamed ü.txt")
        odd_name = "line\nname.txt" if os.name == "posix" else "line name.txt"
        (self.root / odd_name).write_text("x")
        data = cp.inspect(self.root); codes = " ".join(i["status"] for i in data["manifest"])
        self.assertGreaterEqual(data["path_total"], 4); self.assertIn("R", codes)
        if os.name == "posix": self.assertIn("%0A", " ".join(i["path"] for i in data["manifest"]))

    def test_staged_blob_identity_changes_evidence(self):
        (self.root/"tracked.txt").write_text("staged one\n"); git(self.root,"add","tracked.txt"); first=cp.inspect(self.root)
        (self.root/"tracked.txt").write_text("staged two\n"); git(self.root,"add","tracked.txt"); second=cp.inspect(self.root)
        self.assertNotEqual(first["fingerprint"],second["fingerprint"])
        self.assertNotEqual(first["manifest"][0]["index"],second["manifest"][0]["index"])

    def test_unmerged_stage_blob_identities_affect_evidence(self):
        git(self.root,"checkout","-qb","side"); (self.root/"tracked.txt").write_text("side\n"); git(self.root,"commit","-am","side","-q")
        git(self.root,"checkout","-q",self.initial_branch); (self.root/"tracked.txt").write_text("main\n"); git(self.root,"commit","-am","main","-q")
        subprocess.run(["git","-C",str(self.root),"merge","side"],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        first=cp.inspect(self.root); item=next(i for i in first["manifest"] if i["path"]=="tracked.txt")
        self.assertEqual(len(item["index"].split(":")),3)
        blob=subprocess.run(["git","-C",str(self.root),"hash-object","-w","--stdin"],input=b"replacement\n",check=True,stdout=subprocess.PIPE).stdout.strip().decode()
        subprocess.run(["git","-C",str(self.root),"update-index","--index-info"],input=f"100644 {blob} 2\ttracked.txt\n".encode(),check=True)
        second=cp.inspect(self.root); self.assertNotEqual(first["fingerprint"],second["fingerprint"])

    @unittest.skipUnless(os.name == "posix", "POSIX byte filenames")
    def test_undecodable_and_control_filename(self):
        raw = os.fsencode(self.root) + b"/bad-\xff-\x01"
        try: fd = os.open(raw, os.O_CREAT | os.O_WRONLY, 0o600)
        except OSError as e: self.skipTest(f"filesystem rejects undecodable names: {e}")
        os.write(fd, b"x"); os.close(fd)
        paths = [i["path"] for i in cp.inspect(self.root)["manifest"]]
        self.assertTrue(any("%FF" in p and "%01" in p for p in paths))

    def test_unborn_and_detached(self):
        other = Path(self.temp.name) / "unborn"; other.mkdir(); git(other, "init", "-q")
        self.assertEqual(cp.inspect(other)["mode"], "unborn")
        git(self.root, "checkout", "--detach", "-q")
        self.assertEqual(cp.inspect(self.root)["mode"], "detached")

    def test_hostile_commit_subject_decodes_with_replacement(self):
        tree=git(self.root,"rev-parse","HEAD^{tree}").stdout.strip()
        raw=b"tree "+tree+b"\nauthor Test <test@example.com> 0 +0000\ncommitter Test <test@example.com> 0 +0000\n\nbad-\xff-subject\n"
        commit=subprocess.run(["git","-C",str(self.root),"hash-object","-t","commit","-w","--stdin"],input=raw,check=True,stdout=subprocess.PIPE).stdout.strip()
        git(self.root,"update-ref","HEAD",commit.decode())
        self.assertIn("\ufffd",cp.inspect(self.root)["subject"])

    def test_non_git_and_nested_root_rejected(self):
        plain = Path(self.temp.name) / "plain"; plain.mkdir()
        with self.assertRaisesRegex(cp.CheckpointError, "Not a Git worktree"): cp.inspect(plain)
        nested = self.root / "folder"; nested.mkdir()
        with self.assertRaisesRegex(cp.CheckpointError, "worktree root"): cp.inspect(nested)

    def test_discovery_depth_cap_worktree_and_nested(self):
        nested = self.root / "nested"; nested.mkdir(); git(nested, "init", "-q")
        deep = Path(self.temp.name) / "a/b/c/d"; deep.mkdir(parents=True); git(deep, "init", "-q")
        result = cp.discover(self.temp.name)
        self.assertIn("repo", [x["path"] for x in result["candidates"]]); self.assertIn("repo/nested", [x["path"] for x in result["candidates"]]); self.assertNotIn("a/b/c/d", [x["path"] for x in result["candidates"]])
        many=Path(self.temp.name)/"many"; many.mkdir()
        for i in range(51): (many/f"r{i}"/".git").mkdir(parents=True)
        capped=cp.discover(many); self.assertTrue(capped["capped"]); self.assertEqual(len(capped["candidates"]),50)

    def test_worktree_and_submodule_are_opaque_candidates(self):
        worktree = Path(self.temp.name) / "wt"; git(self.root, "worktree", "add", "-q", "-b", "wt", str(worktree))
        candidates=cp.discover(self.temp.name)["candidates"]; self.assertTrue(any(x["path"] == "wt" and x["kind"] == "worktree" for x in candidates))
        child = Path(self.temp.name) / "child"; child.mkdir(); git(child, "init", "-q"); git(child, "config", "user.email", "x@y"); git(child, "config", "user.name", "x"); (child/"x").write_text("x"); git(child,"add","."); git(child,"commit","-qm","x")
        git(self.root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "sub")
        candidates=cp.discover(self.temp.name)["candidates"]; self.assertTrue(any(x["path"] == "repo/sub" and x["kind"] == "submodule" for x in candidates))
        (self.root/"sub/x").write_text("dirty")
        data = cp.inspect(self.root); self.assertTrue(any(i["path"] == "sub" for i in data["manifest"]))

    def test_regular_symlink_fifo_and_no_follow(self):
        (self.root/"regular").write_text("x"); (self.root/"link").symlink_to("regular")
        if hasattr(os, "mkfifo"): os.mkfifo(self.root/"pipe")
        items = cp.inspect(self.root)["manifest"]
        self.assertTrue(any(i["content"].startswith("file:") for i in items)); self.assertTrue(any(i["content"].startswith("symlink:") for i in items))
        if hasattr(os, "mkfifo"): self.assertTrue(cp.safe_hash(self.root,b"pipe",0,cp.time.monotonic()+10)[0].startswith("special:"))

    def test_safe_hash_detects_replacement_and_mutation(self):
        p = self.root/"race"; p.write_text("x")
        real_fstat = os.fstat
        with mock.patch.object(cp.os, "fstat", side_effect=lambda fd: type("S", (), {**{n:getattr(real_fstat(fd),n) for n in ("st_mode","st_dev","st_ino","st_size","st_mtime_ns","st_ctime_ns")}, "st_ino": real_fstat(fd).st_ino + 1})()):
            with self.assertRaisesRegex(cp.CheckpointError, "safe open"): cp.safe_hash(self.root, b"race", 0, cp.time.monotonic()+10)
        calls = 0
        def changing(fd):
            nonlocal calls; calls += 1; s = real_fstat(fd)
            if calls > 1: os.utime(p, None)
            return real_fstat(fd)
        with mock.patch.object(cp.os, "fstat", side_effect=changing):
            with self.assertRaisesRegex(cp.CheckpointError, "changed while hashing"): cp.safe_hash(self.root, b"race", 0, cp.time.monotonic()+10)

    def test_hash_limits_and_elapsed(self):
        p=self.root/"large"; p.write_text("12345")
        with mock.patch.object(cp, "MAX_FILE", 4):
            with self.assertRaisesRegex(cp.CheckpointError, "512 MiB"): cp.inspect(self.root)
        with mock.patch.object(cp, "MAX_TOTAL", 4):
            with self.assertRaisesRegex(cp.CheckpointError, "2 GiB"): cp.inspect(self.root)
        with self.assertRaisesRegex(cp.CheckpointError, "30 seconds"): cp.safe_hash(self.root, b"large", 0, cp.time.monotonic()-1)

    def test_publish_resume_and_handoff_changes_do_not_drift(self):
        evidence = cp.publish(self.root, self.draft)
        text = (self.root/"HANDOFF.md").read_text(); self.assertLessEqual(len(text.splitlines()),120); self.assertLessEqual(len(text.encode()),cp.MAX_HANDOFF)
        resumed=cp.resume(self.root); self.assertTrue(resumed["fresh"]); self.assertEqual(resumed["resume_status"],"fresh"); self.assertNotIn("metadata",resumed); self.assertEqual(resumed["goal"],"Ship v0.1."); self.assertEqual(resumed["next_action"],"Run the unit tests.")
        self.assertEqual(resumed["capabilities"],self.draft["capabilities"]); self.assertEqual(resumed["decisions"],self.draft["decisions"]); self.assertEqual(resumed["acceptance_criteria"],self.draft["acceptance_criteria"]); self.assertEqual(resumed["verification"],self.draft["verification"]); self.assertEqual(resumed["open_questions"],self.draft["open_questions"]); self.assertEqual(resumed["remainder"],self.draft["remainder"])
        (self.root/"HANDOFF.md").write_text(text)
        self.assertEqual(evidence["fingerprint"], cp.inspect(self.root)["fingerprint"])
        (self.root/"tracked.txt").write_text("drift")
        result=cp.resume(self.root); self.assertFalse(result["fresh"]); self.assertIn("tracked.txt", result["changed_paths"])

    def test_new_commit_drift(self):
        cp.publish(self.root,self.draft); git(self.root,"commit","--allow-empty","-qm","empty")
        empty=cp.resume(self.root); self.assertEqual(empty["resume_status"],"advanced"); self.assertEqual(empty["changed_paths"],[])
        (self.root/"x").write_text("x"); git(self.root,"add","x"); git(self.root,"commit","-qm","drift")
        result=cp.resume(self.root); self.assertFalse(result["fresh"]); self.assertEqual(result["resume_status"],"advanced"); self.assertEqual(result["branch_relation"],"advanced"); self.assertEqual((result["ahead"],result["behind"]),(2,0)); self.assertTrue(result["complete_path_comparison"]); self.assertIn("x",result["changed_paths"]); self.assertNotEqual(result["saved_commit"],result["current_commit"]); self.assertEqual(result["saved_branch"],result["current_branch"])

    def test_new_branch_inherits_checkpoint(self):
        cp.publish(self.root,self.draft); git(self.root,"checkout","-qb","other")
        result=cp.resume(self.root); self.assertEqual(result["resume_status"],"inherited"); self.assertEqual(result["branch_relation"],"branch-created"); self.assertEqual((result["ahead"],result["behind"]),(0,0)); self.assertEqual(result["changed_paths"],[]); self.assertTrue(result["complete_path_comparison"])
        (self.root/"tracked.txt").write_text("changed")
        drifted=cp.resume(self.root); self.assertEqual(drifted["resume_status"],"drifted"); self.assertEqual(drifted["branch_relation"],"branch-created"); self.assertIn("tracked.txt",drifted["changed_paths"])

    def test_rewound_and_diverged_branches_are_classified(self):
        initial=git(self.root,"rev-parse","HEAD").stdout.strip().decode(); (self.root/"side.txt").write_text("side"); git(self.root,"add","side.txt"); git(self.root,"commit","-qm","side")
        cp.publish(self.root,self.draft); git(self.root,"checkout","-q",initial)
        rewound=cp.resume(self.root); self.assertEqual(rewound["branch_relation"],"rewound"); self.assertEqual((rewound["ahead"],rewound["behind"]),(0,1)); self.assertIn("side.txt",rewound["changed_paths"])
        git(self.root,"checkout","-qb","other"); (self.root/"other.txt").write_text("other"); git(self.root,"add","other.txt"); git(self.root,"commit","-qm","other")
        diverged=cp.resume(self.root); self.assertEqual(diverged["branch_relation"],"diverged"); self.assertEqual((diverged["ahead"],diverged["behind"]),(1,1)); self.assertEqual(set(diverged["changed_paths"]),{"other.txt","side.txt"})

    def test_manual_prose_and_metadata_edits_rejected(self):
        cp.publish(self.root,self.draft); p=self.root/"HANDOFF.md"; text=p.read_text()
        with self.assertRaisesRegex(cp.CheckpointError,"prose was edited"): cp.parse_handoff(text.replace("Ship v0.1.","Ship v0.2."))
        line=next(x for x in text.splitlines() if x.startswith(cp.META_PREFIX)); token=line[len(cp.META_PREFIX):-4]
        with self.assertRaises(Exception): cp.parse_handoff(text.replace(token, ("A" if token[0] != "A" else "B") + token[1:], 1))

    def test_unowned_symlink_and_changed_target_refused(self):
        target=self.root/"HANDOFF.md"; target.write_text("mine")
        with self.assertRaisesRegex(cp.CheckpointError,"explicitly approve"): cp.publish(self.root,self.draft)
        target.unlink(); target.symlink_to("tracked.txt")
        with self.assertRaisesRegex(cp.CheckpointError,"regular file"): cp.publish(self.root,self.draft)
        target.unlink(); target.write_text("mine"); auth=cp.authorize_target(target,True); target.write_text("changed")
        self.assertFalse(cp.target_unchanged(target,auth))

    def test_handoff_safe_read_refuses_swap_special_and_growth(self):
        target=self.root/"HANDOFF.md"; target.write_text("safe")
        real_open=os.open
        def swap(path,flags,*args):
            target.unlink(); target.symlink_to("tracked.txt"); return real_open(path,flags,*args)
        with mock.patch.object(cp.os,"open",side_effect=swap):
            with self.assertRaises(cp.CheckpointError): cp.read_regular(target)
        target.unlink()
        if hasattr(os,"mkfifo"):
            os.mkfifo(target)
            with self.assertRaisesRegex(cp.CheckpointError,"regular file"): cp.read_regular(target)
            target.unlink()
        target.write_bytes(b"12345")
        with self.assertRaisesRegex(cp.CheckpointError,"bounded read"): cp.read_regular(target,4)

    def test_target_absence_change_detected(self):
        target=self.root/"HANDOFF.md"; auth=cp.authorize_target(target,False); target.write_text("appeared")
        self.assertFalse(cp.target_unchanged(target,auth))

    def test_references_plan_symlink_and_escape(self):
        (self.root/"PLAN.md").write_text("locked"); (self.root/"doc.md").write_text("doc")
        draft={**self.draft,"references":["doc.md"]}; cp.publish(self.root,draft); self.assertEqual(cp.parse_handoff((self.root/"HANDOFF.md").read_text())["plan"],"PLAN.md")
        (self.root/"link.md").symlink_to("doc.md")
        for ref in ("link.md","../outside"):
            with self.assertRaises(cp.CheckpointError): cp.validate_refs(self.root,[ref])
        (self.root/"dir").mkdir(); (self.root/"dir/real.md").write_text("x"); (self.root/"alias").symlink_to("dir",target_is_directory=True)
        with self.assertRaisesRegex(cp.CheckpointError,"symlinks"): cp.validate_refs(self.root,["alias/real.md"])
        (self.root/"PLAN.md").unlink(); (self.root/"PLAN.md").symlink_to("doc.md")
        with self.assertRaisesRegex(cp.CheckpointError,"symlinks"): cp.render(self.root,self.draft,cp.inspect(self.root))

    def test_required_sections_placeholders_secret_and_size_limits(self):
        cp.publish(self.root,self.draft); text=(self.root/"HANDOFF.md").read_text()
        with self.assertRaisesRegex(cp.CheckpointError,"section"): cp.validate_handoff(text.replace("## Identity","## Missing"))
        with self.assertRaisesRegex(cp.CheckpointError,"placeholder"): cp.render(self.root,{**self.draft,"goal":"TODO"},cp.inspect(self.root))
        with self.assertRaisesRegex(cp.CheckpointError,"secret"): cp.render(self.root,{**self.draft,"goal":"api_key=supersecretvalue"},cp.inspect(self.root))
        with mock.patch.object(cp,"MAX_HANDOFF",100):
            with self.assertRaisesRegex(cp.CheckpointError,"24 KiB"): cp.validate_handoff(text)
        with mock.patch.object(cp,"MAX_META",10):
            with self.assertRaisesRegex(cp.CheckpointError,"12 KiB"): cp.render(self.root,self.draft,cp.inspect(self.root))

    def test_secret_forms_and_verification_sanitization(self):
        for secret in ("ghp_abcdefghijklmnop", "github_pat_abcdefghijklmnop", "sk-abcdefghijklmnop"):
            with self.assertRaisesRegex(cp.CheckpointError,"secret"): cp.reject_secrets(secret)
        draft={**self.draft,"verification":["TOKEN=abc pytest --token hidden --password=secret"]}
        text=cp.render(self.root,draft,cp.inspect(self.root)); self.assertIn("[env redacted]",text); self.assertIn("--token [redacted]",text); self.assertIn("--password [redacted]",text); self.assertNotIn("hidden",text)

    def test_draft_and_metadata_types_fail_cleanly(self):
        for draft in ([], {**self.draft,"decisions":"no"}, {**self.draft,"capabilities":[1]}, {**self.draft,"extra":1}):
            with self.assertRaises(cp.CheckpointError): cp.render(self.root,draft,cp.inspect(self.root))
        cp.publish(self.root,self.draft); meta=cp.parse_handoff((self.root/"HANDOFF.md").read_text())
        for change in ({"commit":3},{"manifest":{}},{"path_total":"1"},{"path_total":1},{"plan":"OTHER.md"},{"extra":1}):
            bad={**meta,**change}
            with self.assertRaises(cp.CheckpointError): cp.validate_metadata(bad)

    def test_metadata_manifest_shapes_and_path_round_trip(self):
        (self.root/"odd ü.txt").write_text("x"); data=cp.inspect(self.root); text=cp.render(self.root,self.draft,data); meta=cp.parse_handoff(text); item=meta["manifest"][0]
        self.assertNotIn(item["index"], cp.prose_without_meta(text)); self.assertNotIn(item["content"], cp.prose_without_meta(text)); self.assertIn(f"`??` `{item['path']}`", text)
        cp.validate_metadata({**meta,"manifest":[{**item,"status":"T."}]})
        for change in ({"path_b64":item["path_b64"]+"="},{"path":"wrong"},{"status":"bad"},{"index":"no"},{"content":"file:no"}):
            bad={**meta,"manifest":[{**item,**change}]}
            with self.assertRaises(cp.CheckpointError): cp.validate_metadata(bad)
        bad={**meta,"manifest":[{**item,"old_path":"orphan"}]}
        with self.assertRaises(cp.CheckpointError): cp.validate_metadata(bad)

    def test_skill_manifest_without_yaml_dependency(self):
        skill=Path(__file__).parents[1]/"skills/project-checkpoint/SKILL.md"; text=skill.read_text()
        front=text.split("---",2)[1]
        self.assertIn("\nname: project-checkpoint\n",front); self.assertIn("\ndescription:",front)
        agent=(skill.parent/"agents/openai.yaml").read_text()
        self.assertIn('display_name: "Project Checkpoint"',agent); self.assertIn('short_description: "Save and resume integrity-checked project state"',agent); self.assertIn('$project-checkpoint',agent)
        self.assertNotIn("python3 scripts/checkpoint.py",text); self.assertIn('<python> "<checkpoint.py>"',text); self.assertIn("Python 3.10+",text); self.assertIn("Reconcile every claim",text); self.assertIn("repository-verified",text); self.assertIn("potentially stale",text); self.assertIn("OS temporary directory outside",text)

    def test_absolute_script_cli_inspect_publish_resume(self):
        draft=Path(self.temp.name)/"draft.json"; draft.write_text(json.dumps(self.draft))
        def cli(*args):
            p=subprocess.run([sys.executable,os.fspath(SCRIPT.resolve()),*args],check=True,stdout=subprocess.PIPE,text=True,encoding="utf-8")
            return json.loads(p.stdout)
        self.assertEqual(cli("inspect","--project",str(self.root))["path_total"],0)
        published=cli("publish","--project",str(self.root),"--input",str(draft)); self.assertTrue(published["published"]); self.assertNotIn("manifest",published)
        draft.unlink()
        resumed=cli("resume","--project",str(self.root)); self.assertTrue(resumed["fresh"]); self.assertNotIn("metadata",resumed); self.assertEqual(resumed["current_state"],"Implementation is active.")

    def test_bounded_large_manifest_and_overflow_resume(self):
        for i in range(cp.MAX_MANIFEST+3): (self.root/f"u{i}").write_text("x")
        data=cp.inspect(self.root); self.assertEqual(len(data["manifest"]),cp.MAX_MANIFEST); self.assertEqual(data["path_omitted"],3)
        cp.publish(self.root,self.draft); result=cp.resume(self.root); self.assertFalse(result["complete_path_comparison"])

    def test_repository_change_before_publish_aborts(self):
        real=cp.inspect; calls=0
        def drift(root):
            nonlocal calls; calls+=1; data=real(root)
            if calls==2: data={**data,"fingerprint":"0"*64}
            return data
        with mock.patch.object(cp,"inspect",side_effect=drift):
            with self.assertRaisesRegex(cp.CheckpointError,"changed before publication"): cp.publish(self.root,self.draft)

if __name__ == "__main__": unittest.main()
