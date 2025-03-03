import argparse
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Tuple

from tools.stats.upload_stats_lib import (
    download_gha_artifacts,
    download_s3_artifacts,
    is_rerun_disabled_tests,
    unzip,
    upload_workflow_stats_to_s3,
)


def get_job_id(report: Path) -> int:
    # [Job id in artifacts]
    # Retrieve the job id from the report path. In our GHA workflows, we append
    # the job id to the end of the report name, so `report` looks like:
    #     unzipped-test-reports-foo_5596745227/test/test-reports/foo/TEST-foo.xml
    # and we want to get `5596745227` out of it.
    return int(report.parts[0].rpartition("_")[2])


def parse_xml_report(
    tag: str,
    report: Path,
    workflow_id: int,
    workflow_run_attempt: int,
) -> List[Dict[str, Any]]:
    """Convert a test report xml file into a JSON-serializable list of test cases."""
    print(f"Parsing {tag}s for test report: {report}")

    try:
        job_id = get_job_id(report)
        print(f"Found job id: {job_id}")
    except Exception:
        job_id = None
        print("Failed to find job id")

    test_cases: List[Dict[str, Any]] = []

    root = ET.parse(report)
    # TODO: unlike unittest, pytest-flakefinder used by rerun disabled tests for test_ops
    # includes skipped messages multiple times (50 times by default). This slows down
    # this script too much (O(n)) because it tries to gather all the stats. This should
    # be fixed later in the way we use pytest-flakefinder. A zipped test report from rerun
    # disabled test is only few MB, but will balloon up to a much bigger XML file after
    # extracting from a dozen to few hundred MB
    if is_rerun_disabled_tests(root):
        return test_cases

    for test_case in root.iter(tag):
        case = process_xml_element(test_case)
        case["workflow_id"] = workflow_id
        case["workflow_run_attempt"] = workflow_run_attempt
        case["job_id"] = job_id

        # [invoking file]
        # The name of the file that the test is located in is not necessarily
        # the same as the name of the file that invoked the test.
        # For example, `test_jit.py` calls into multiple other test files (e.g.
        # jit/test_dce.py). For sharding/test selection purposes, we want to
        # record the file that invoked the test.
        #
        # To do this, we leverage an implementation detail of how we write out
        # tests (https://bit.ly/3ajEV1M), which is that reports are created
        # under a folder with the same name as the invoking file.
        case["invoking_file"] = report.parent.name
        test_cases.append(case)

    return test_cases


def process_xml_element(element: ET.Element) -> Dict[str, Any]:
    """Convert a test suite element into a JSON-serializable dict."""
    ret: Dict[str, Any] = {}

    # Convert attributes directly into dict elements.
    # e.g.
    #     <testcase name="test_foo" classname="test_bar"></testcase>
    # becomes:
    #     {"name": "test_foo", "classname": "test_bar"}
    ret.update(element.attrib)

    # The XML format encodes all values as strings. Convert to ints/floats if
    # possible to make aggregation possible in Rockset.
    for k, v in ret.items():
        try:
            ret[k] = int(v)
        except ValueError:
            pass
        try:
            ret[k] = float(v)
        except ValueError:
            pass

    # Convert inner and outer text into special dict elements.
    # e.g.
    #     <testcase>my_inner_text</testcase> my_tail
    # becomes:
    #     {"text": "my_inner_text", "tail": " my_tail"}
    if element.text and element.text.strip():
        ret["text"] = element.text
    if element.tail and element.tail.strip():
        ret["tail"] = element.tail

    # Convert child elements recursively, placing them at a key:
    # e.g.
    #     <testcase>
    #       <foo>hello</foo>
    #       <foo>world</foo>
    #       <bar>another</bar>
    #     </testcase>
    # becomes
    #    {
    #       "foo": [{"text": "hello"}, {"text": "world"}],
    #       "bar": {"text": "another"}
    #    }
    for child in element:
        if child.tag not in ret:
            ret[child.tag] = process_xml_element(child)
        else:
            # If there are multiple tags with the same name, they should be
            # coalesced into a list.
            if not isinstance(ret[child.tag], list):
                ret[child.tag] = [ret[child.tag]]
            ret[child.tag].append(process_xml_element(child))
    return ret


def get_pytest_parallel_times() -> Dict[Any, Any]:
    pytest_parallel_times: Dict[Any, Any] = {}
    for report in Path(".").glob("**/python-pytest/**/*.xml"):
        invoking_file = report.parent.name

        root = ET.parse(report)
        # TODO: Skip test reports from rerun disabled tests, same reason as mentioned
        # above
        if is_rerun_disabled_tests(root):
            continue

        assert len(list(root.iter("testsuite"))) == 1
        for test_suite in root.iter("testsuite"):
            pytest_parallel_times[
                (invoking_file, get_job_id(report))
            ] = test_suite.attrib["time"]
    return pytest_parallel_times


def get_tests(
    workflow_run_id: int, workflow_run_attempt: int
) -> Tuple[List[Dict[str, Any]], Dict[Any, Any]]:
    with TemporaryDirectory() as temp_dir:
        print("Using temporary directory:", temp_dir)
        os.chdir(temp_dir)

        # Download and extract all the reports (both GHA and S3)
        s3_paths = download_s3_artifacts(
            "test-report", workflow_run_id, workflow_run_attempt
        )
        for path in s3_paths:
            unzip(path)

        artifact_paths = download_gha_artifacts(
            "test-report", workflow_run_id, workflow_run_attempt
        )
        for path in artifact_paths:
            unzip(path)

        # Parse the reports and transform them to JSON
        test_cases = []
        for xml_report in Path(".").glob("**/*.xml"):
            test_cases.extend(
                parse_xml_report(
                    "testcase",
                    xml_report,
                    workflow_run_id,
                    workflow_run_attempt,
                )
            )

        pytest_parallel_times = get_pytest_parallel_times()

        return test_cases, pytest_parallel_times


def get_tests_for_circleci(
    workflow_run_id: int, workflow_run_attempt: int
) -> Tuple[List[Dict[str, Any]], Dict[Any, Any]]:
    # Parse the reports and transform them to JSON
    test_cases = []
    for xml_report in Path(".").glob("**/test/test-reports/**/*.xml"):
        test_cases.extend(
            parse_xml_report(
                "testcase", xml_report, workflow_run_id, workflow_run_attempt
            )
        )

    pytest_parallel_times = get_pytest_parallel_times()

    return test_cases, pytest_parallel_times


def get_invoking_file_times(
    test_case_summaries: List[Dict[str, Any]], pytest_parallel_times: Dict[Any, Any]
) -> List[Dict[str, Any]]:
    def get_key(summary: Dict[str, Any]) -> Any:
        return (
            summary["invoking_file"],
            summary["job_id"],
        )

    def init_value(summary: Dict[str, Any]) -> Any:
        return {
            "job_id": summary["job_id"],
            "workflow_id": summary["workflow_id"],
            "workflow_run_attempt": summary["workflow_run_attempt"],
            "invoking_file": summary["invoking_file"],
            "time": 0.0,
        }

    ret = {}
    for summary in test_case_summaries:
        key = get_key(summary)
        if key not in ret:
            ret[key] = init_value(summary)
        ret[key]["time"] += summary["time"]

    for key, val in ret.items():
        # when running in parallel in pytest, adding the test times will not give the correct
        # time used to run the file, which will make the sharding incorrect, so if the test is
        # run in parallel, we take the time reported by the testsuite
        if key in pytest_parallel_times:
            val["time"] = pytest_parallel_times[key]

    return list(ret.values())


def summarize_test_cases(test_cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group test cases by classname, file, and job_id. We perform the aggregation
    manually instead of using the `test-suite` XML tag because xmlrunner does
    not produce reliable output for it.
    """

    def get_key(test_case: Dict[str, Any]) -> Any:
        return (
            test_case.get("file"),
            test_case.get("classname"),
            test_case["job_id"],
            test_case["workflow_id"],
            test_case["workflow_run_attempt"],
            # [see: invoking file]
            test_case["invoking_file"],
        )

    def init_value(test_case: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "file": test_case.get("file"),
            "classname": test_case.get("classname"),
            "job_id": test_case["job_id"],
            "workflow_id": test_case["workflow_id"],
            "workflow_run_attempt": test_case["workflow_run_attempt"],
            # [see: invoking file]
            "invoking_file": test_case["invoking_file"],
            "tests": 0,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "successes": 0,
            "time": 0.0,
        }

    ret = {}
    for test_case in test_cases:
        key = get_key(test_case)
        if key not in ret:
            ret[key] = init_value(test_case)

        ret[key]["tests"] += 1

        if "failure" in test_case:
            ret[key]["failures"] += 1
        elif "error" in test_case:
            ret[key]["errors"] += 1
        elif "skipped" in test_case:
            ret[key]["skipped"] += 1
        else:
            ret[key]["successes"] += 1

        ret[key]["time"] += test_case["time"]
    return list(ret.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload test stats to Rockset")
    parser.add_argument(
        "--workflow-run-id",
        required=True,
        help="id of the workflow to get artifacts from",
    )
    parser.add_argument(
        "--workflow-run-attempt",
        type=int,
        required=True,
        help="which retry of the workflow this is",
    )
    parser.add_argument(
        "--head-branch",
        required=True,
        help="Head branch of the workflow",
    )
    parser.add_argument(
        "--circleci",
        action="store_true",
        help="If this is being run through circleci",
    )
    args = parser.parse_args()

    print(f"Workflow id is: {args.workflow_run_id}")

    if args.circleci:
        test_cases, pytest_parallel_times = get_tests_for_circleci(
            args.workflow_run_id, args.workflow_run_attempt
        )
    else:
        test_cases, pytest_parallel_times = get_tests(
            args.workflow_run_id, args.workflow_run_attempt
        )

    # Flush stdout so that any errors in Rockset upload show up last in the logs.
    sys.stdout.flush()

    # For PRs, only upload a summary of test_runs. This helps lower the
    # volume of writes we do to Rockset.
    test_case_summary = summarize_test_cases(test_cases)
    invoking_file_times = get_invoking_file_times(
        test_case_summary, pytest_parallel_times
    )

    upload_workflow_stats_to_s3(
        args.workflow_run_id,
        args.workflow_run_attempt,
        "test_run_summary",
        test_case_summary,
    )

    upload_workflow_stats_to_s3(
        args.workflow_run_id,
        args.workflow_run_attempt,
        "invoking_file_times",
        invoking_file_times,
    )

    # Separate out the failed test cases.
    # Uploading everything is too data intensive most of the time,
    # but these will be just a tiny fraction.
    failed_tests_cases = []
    for test_case in test_cases:
        if "rerun" in test_case or "failure" in test_case or "error" in test_case:
            failed_tests_cases.append(test_case)

    upload_workflow_stats_to_s3(
        args.workflow_run_id,
        args.workflow_run_attempt,
        "failed_test_runs",
        failed_tests_cases,
    )

    if args.head_branch == "main":
        # For jobs on main branch, upload everything.
        upload_workflow_stats_to_s3(
            args.workflow_run_id, args.workflow_run_attempt, "test_run", test_cases
        )
