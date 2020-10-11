import re
from dataclasses import dataclass

TRACEBACK_REGEX = re.compile(
    r"""
     ^Traceback # Lines starting with "Traceback"
     [\s\S]+? # Match greedy any text (preserving ASCII flags).
     (?=^(?:\d|test|\Z|\n|ok)) # Stop matching in lines starting with
                            # a number (log time), "test" or the end
                            # of the string.
    """,
    re.MULTILINE | re.VERBOSE,
)

LEAKS_REGEX = re.compile(r"(test_\w+) leaked \[.*]\ (.*),.*", re.MULTILINE)


@dataclass
class SubTest:
    name: str
    parent: str

    def __eq__(self, other):
        if not isinstance(other, SubTest):
            return False
        return self.name == other.name

    def __hash__(self):
        return hash(self.name)


@dataclass
class Test:
    name: str

    def __hash__(self):
        return hash(self.name)


@dataclass
class LeakTest(Test):
    resource: str

    def __hash__(self):
        return hash(self.name)


class Logs:
    def __init__(self, raw_logs):
        self._logs = raw_logs

    @property
    def raw_logs(self):
        return self._logs

    def _get_test_results(self, header):
        test_regexp = re.compile(
            rf"""
             ^\d+\s{header}: # Lines starting with "Traceback"
             [\s\S]+? # Match greedy any text (preserving ASCII flags).
             (?=^(?:\d|test|\Z|Total)) # Stop matching in lines starting with
                                 # a number (log time), "test" or the end
                                 # of the string.
            """,
            re.MULTILINE | re.VERBOSE,
        )

        failed_blocks = list(set(test_regexp.findall(self._logs)))
        if not failed_blocks:
            return set()
        # Pick the last re-run of the test
        block = failed_blocks[-1]
        tests = []
        for line in block.split("\n")[1:]:
            if not line:
                continue
            test_names = line.split(" ")
            tests.extend(test for test in test_names if test)
        return set(tests)

    def get_tracebacks(self):
        for traceback in set(TRACEBACK_REGEX.findall(self._logs)):
            yield traceback

    def get_leaks(self):
        for test_name, resource in set(LEAKS_REGEX.findall(self._logs)):
            yield LeakTest(test_name, resource)

    def get_failed_tests(self):
        for test_name in set(self._get_test_results(r"tests?\sfailed")):
            yield Test(test_name)

    def get_rerun_tests(self):
        for test_name in set(self._get_test_results(r"re-run\stests?")):
            yield Test(test_name)

    def get_failed_subtests(self):
        failed_subtest_regexp = re.compile(
            r"=+"  # Decoration prefix
            r"\n[A-Z]+:"  # Test result (e.g. FAIL:)
            r"\s(\w+)\s"  # test name (e.g. test_tools)
            r"\((.*?)\)"  # subtest name (e.g. test.test_tools.test_unparse.DirectoryTestCase)
            r".*"  # Trailing text (e.g. filename)
            r"\n*"  # End of the line
            r".*"  # Maybe some test description
            r"-+",  # Trailing decoration
            re.MULTILINE | re.VERBOSE,
        )
        for test, subtest in set(failed_subtest_regexp.findall(self._logs)):
            yield SubTest(parent=test, name=subtest)

    def test_summary(self):
        if not self._logs:
            return ""
        result_start = [
            match.start() for match in re.finditer("== Tests result", self._logs)
        ][-1]
        result_end = [
            match.start() for match in re.finditer("Tests result:", self._logs)
        ][-1]
        return self._logs[result_start:result_end]
