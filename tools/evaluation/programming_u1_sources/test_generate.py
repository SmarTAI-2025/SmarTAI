from hashlib import sha256
import shutil

import pytest

from tools.evaluation.programming_u1_sources import generate


def test_v2_projections_match_frozen_hashes_without_reading_generated_files(tmp_path):
    packet = tmp_path / "inputs"
    report = "validation/local_fixture_results_v2.json"
    (packet / report).parent.mkdir(parents=True)
    shutil.copyfile(generate.PACKET / report, packet / report)

    output = tmp_path / "derived"
    generate.generate(packet, output)
    checksums = {}
    for line in (generate.PACKET / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        checksums[name] = digest
    actual = {
        p.relative_to(output).as_posix(): sha256(p.read_bytes()).hexdigest()
        for p in output.rglob("*")
        if p.is_file()
    }
    generated_directories = {"cases", "references", "submissions", "mapping_inputs", "teacher_inputs"}
    expected = {name for name in checksums if name.split("/", 1)[0] in generated_directories}
    expected.update(
        {
            "questions.json",
            "submissions.json",
            "scenarios.json",
            "QUESTIONS_AND_RULES.md",
            "MANUAL_GRADING_REVIEW.md",
            "MANUAL_GRADING_REVIEW.csv",
        }
    )
    assert len(expected) == 136
    assert actual == {name: checksums[name] for name in expected}


def test_existing_output_preserves_filled_review_sheet(tmp_path):
    sheet = tmp_path / "MANUAL_GRADING_REVIEW.csv"
    sheet.write_bytes(b"existing teacher review")
    with pytest.raises(FileExistsError):
        generate.generate(generate.PACKET, tmp_path)
    assert sheet.read_bytes() == b"existing teacher review"


def test_reject_output_within_packet_through_symlink(tmp_path):
    alias = tmp_path / "packet-link"
    alias.symlink_to(generate.PACKET, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the frozen packet"):
        generate.generate(generate.PACKET, alias / "derived")


def test_reject_case_path_outside_output(tmp_path):
    output = tmp_path / "derived"
    case = {"case_id": "T01", "input": "", "expected": ""}
    question = {"q_id": "../../elsewhere", "mode": "stdin_stdout", "cases": [case]}
    with pytest.raises(ValueError, match="Invalid case path"):
        generate.write_cases(output, [question])
    assert list(tmp_path.iterdir()) == []
