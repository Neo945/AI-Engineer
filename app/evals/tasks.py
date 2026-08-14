"""Benchmark task definitions for the headless evaluation harness.

Each task scaffolds a small, self-contained repository and asks the agent to
fix, extend, or harden it. Fixtures are deliberately stdlib-only
(``unittest``) so the sandbox needs no package installation, and each initial
state fails the accompanying test so a run has a measurable outcome.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from app.executor.paths import PathTraversalError, resolve_within


class EvalTask(BaseModel):
    """A single benchmark scenario.

    Attributes:
        id: Stable identifier used on the command line and in result records.
        name: Short human-readable name.
        category: Area exercised (security, api, data, bug, performance).
        goal: The prompt handed to the agent.
        files: Relative path → file contents used to scaffold the workspace.
        test_command: Command the harness runs to grade the outcome.
        timeout_seconds: Default wall-clock cap for one run of this task.
    """

    id: str
    name: str
    category: str
    goal: str
    files: dict[str, str]
    test_command: str = "python -m unittest -v"
    timeout_seconds: int = 240


_FIX_AUTH_BUG = EvalTask(
    id="fix-auth-bug",
    name="fix auth expiry check",
    category="security",
    goal=(
        "The authorize() function accepts expired tokens and tokens without "
        "an expiry claim. Fix it so a token is only accepted when it carries "
        "an exp claim that has not passed."
    ),
    files={
        "app_auth.py": (
            "import time\n"
            "\n"
            "\n"
            "def authorize(token: dict, now: float | None = None) -> bool:\n"
            '    """Return True when the token is valid and unexpired."""\n'
            "    now = time.time() if now is None else now\n"
            "    if not token or token.get('user') is None:\n"
            "        return False\n"
            "    # FIXME: expiry is never checked\n"
            "    return True\n"
        ),
        "test_auth.py": (
            "import time\n"
            "import unittest\n"
            "from app_auth import authorize\n"
            "\n"
            "\n"
            "class TestAuthorize(unittest.TestCase):\n"
            "    def test_rejects_missing_user(self) -> None:\n"
            "        self.assertFalse(authorize({'exp': time.time() + 100}))\n"
            "\n"
            "    def test_accepts_valid_token(self) -> None:\n"
            "        token = {'user': 'ada', 'exp': time.time() + 100}\n"
            "        self.assertTrue(authorize(token, now=time.time()))\n"
            "\n"
            "    def test_rejects_expired_token(self) -> None:\n"
            "        token = {'user': 'ada', 'exp': time.time() - 5}\n"
            "        self.assertFalse(authorize(token, now=time.time()))\n"
            "\n"
            "    def test_rejects_token_without_expiry(self) -> None:\n"
            "        self.assertFalse(authorize({'user': 'ada'}, now=time.time()))\n"
        ),
    },
)

_ADD_REST_ENDPOINT = EvalTask(
    id="add-rest-endpoint",
    name="add missing /items endpoint",
    category="api",
    goal=(
        "The tiny API is missing its /items endpoint: route() currently "
        "returns 404 for it. Add the endpoint so it returns 200 with the "
        "list of item names as a JSON array. Keep the existing endpoints "
        "working."
    ),
    files={
        "app_api.py": (
            "import json\n"
            "\n"
            "\n"
            "ITEMS = {'1': 'apple', '2': 'banana', '3': 'cherry'}\n"
            "\n"
            "\n"
            "def route(path: str, method: str = 'GET'):\n"
            '    """Dispatch a request and return (status, body)."""\n'
            "    if path == '/health':\n"
            "        return 200, 'ok'\n"
            "    if path == '/version':\n"
            "        return 200, '1.0.0'\n"
            "    # FIXME: /items is missing\n"
            "    return 404, 'not found'\n"
        ),
        "test_api.py": (
            "import json\n"
            "import unittest\n"
            "from app_api import route\n"
            "\n"
            "\n"
            "class TestRoute(unittest.TestCase):\n"
            "    def test_health(self) -> None:\n"
            "        self.assertEqual((200, 'ok'), route('/health'))\n"
            "\n"
            "    def test_version(self) -> None:\n"
            "        self.assertEqual((200, '1.0.0'), route('/version'))\n"
            "\n"
            "    def test_items_lists_names(self) -> None:\n"
            "        status, body = route('/items')\n"
            "        self.assertEqual(200, status)\n"
            "        self.assertEqual(['apple', 'banana', 'cherry'], json.loads(body))\n"
        ),
    },
)

_ADD_DB_MIGRATION = EvalTask(
    id="add-db-migration",
    name="add email column migration",
    category="data",
    goal=(
        "The users schema is missing its email column. Implement migrate() so "
        "it returns a schema that keeps the existing id and username columns "
        "and adds an email column of type varchar(255)."
    ),
    files={
        "app_migrate.py": (
            "USERS_SCHEMA = {\n"
            "    'columns': [\n"
            "        {'name': 'id', 'type': 'int'},\n"
            "        {'name': 'username', 'type': 'varchar(32)'},\n"
            "    ],\n"
            "}\n"
            "\n"
            "\n"
            "def migrate(schema: dict) -> dict:\n"
            '    """Return a copy of schema with the email column added."""\n'
            "    # FIXME: email column is never added\n"
            "    return schema\n"
        ),
        "test_migrate.py": (
            "import unittest\n"
            "from app_migrate import USERS_SCHEMA, migrate\n"
            "\n"
            "\n"
            "class TestMigrate(unittest.TestCase):\n"
            "    def _names(self, schema: dict) -> set:\n"
            "        return {column['name'] for column in schema['columns']}\n"
            "\n"
            "    def test_email_column_added(self) -> None:\n"
            "        names = self._names(migrate(dict(USERS_SCHEMA)))\n"
            "        self.assertIn('email', names)\n"
            "\n"
            "    def test_existing_columns_preserved(self) -> None:\n"
            "        names = self._names(migrate(dict(USERS_SCHEMA)))\n"
            "        self.assertIn('id', names)\n"
            "        self.assertIn('username', names)\n"
        ),
    },
)

_FIX_FAILING_TEST = EvalTask(
    id="fix-failing-test",
    name="fix duration parser",
    category="bug",
    goal=(
        "parse_duration() counts minutes as hours: '1h30m' parses to 1860 "
        "instead of 90. Fix the parsing so hours contribute 60 minutes and "
        "minutes contribute 1 minute each."
    ),
    files={
        "app_duration.py": (
            "def parse_duration(text: str) -> int:\n"
            '    """Parse a duration like \'1h30m\' into total minutes."""\n'
            "    total = 0\n"
            "    value = ''\n"
            "    for char in text:\n"
            "        if char.isdigit():\n"
            "            value += char\n"
            "        elif char == 'h':\n"
            "            total += int(value) * 60\n"
            "            value = ''\n"
            "        elif char == 'm':\n"
            "            total += int(value) * 60  # FIXME: minutes parsed as hours\n"
            "            value = ''\n"
            "    return total\n"
        ),
        "test_duration.py": (
            "import unittest\n"
            "from app_duration import parse_duration\n"
            "\n"
            "\n"
            "class TestParseDuration(unittest.TestCase):\n"
            "    def test_minutes_only(self) -> None:\n"
            "        self.assertEqual(30, parse_duration('30m'))\n"
            "\n"
            "    def test_hours_only(self) -> None:\n"
            "        self.assertEqual(120, parse_duration('2h'))\n"
            "\n"
            "    def test_hours_and_minutes(self) -> None:\n"
            "        self.assertEqual(90, parse_duration('1h30m'))\n"
            "\n"
            "    def test_empty(self) -> None:\n"
            "        self.assertEqual(0, parse_duration(''))\n"
        ),
    },
)

_OPTIMIZE_QUERY = EvalTask(
    id="optimize-query",
    name="optimize two_sum",
    category="performance",
    goal=(
        "two_sum() uses a quadratic double loop. Rewrite it as a single pass "
        "with a hash map so it performs roughly one element access per input "
        "value instead of one per pair."
    ),
    files={
        "app_two_sum.py": (
            "def two_sum(nums: list, target: int):\n"
            '    """Return the indices of two numbers that sum to target."""\n'
            "    for i in range(len(nums)):\n"
            "        for j in range(i + 1, len(nums)):\n"
            "            if nums[i] + nums[j] == target:\n"
            "                return [i, j]\n"
            "    return None\n"
        ),
        "test_two_sum.py": (
            "import unittest\n"
            "from app_two_sum import two_sum\n"
            "\n"
            "\n"
            "class Counted(int):\n"
            "    _adds = 0\n"
            "\n"
            "    def __add__(self, other):\n"
            "        Counted._adds += 1\n"
            "        return int.__add__(self, other)\n"
            "\n"
            "    __hash__ = int.__hash__\n"
            "\n"
            "\n"
            "class TestTwoSum(unittest.TestCase):\n"
            "    def test_finds_pair(self) -> None:\n"
            "        self.assertEqual([0, 2], two_sum([1, 2, 3], 4))\n"
            "\n"
            "    def test_no_pair_returns_none(self) -> None:\n"
            "        self.assertIsNone(two_sum([1, 2, 3], 100))\n"
            "\n"
            "    def test_scales_linearly(self) -> None:\n"
            "        Counted._adds = 0\n"
            "        nums = [Counted(i) for i in range(200)]\n"
            "        self.assertIsNone(two_sum(nums, 10_000))\n"
            "        self.assertLess(Counted._adds, 600)\n"
        ),
    },
)

_FIND_SECURITY_ISSUE = EvalTask(
    id="find-security-issue",
    name="harden SQL injection",
    category="security",
    goal=(
        "build_user_query() interpolates untrusted input straight into a SQL "
        "string, enabling SQL injection. Fix it so malicious payloads can "
        "never appear in the returned query: sanitize or parameterize the "
        "input before building the query."
    ),
    files={
        "app_security.py": (
            "def build_user_query(user_id: str) -> str:\n"
            '    """Return the SQL that looks a user up by id."""\n'
            "    return f\"SELECT * FROM users WHERE id = '{user_id}'\"\n"
        ),
        "test_security.py": (
            "import unittest\n"
            "from app_security import build_user_query\n"
            "\n"
            "\n"
            "class TestBuildUserQuery(unittest.TestCase):\n"
            "    def test_plain_id_included(self) -> None:\n"
            "        self.assertIn('42', build_user_query('42'))\n"
            "\n"
            "    def test_malicious_payload_never_interpolated(self) -> None:\n"
            "        payload = \"1' OR '1'='1\"\n"
            "        sql = build_user_query(payload)\n"
            "        self.assertNotIn(payload, sql)\n"
            "        self.assertNotIn('OR', sql)\n"
            "\n"
            "    def test_sql_keywords_never_present(self) -> None:\n"
            '        sql = build_user_query("1\'; DROP TABLE users;--")\n'
            "        self.assertNotIn('DROP', sql)\n"
        ),
    },
)

BENCHMARK_TASKS: list[EvalTask] = [
    _FIX_AUTH_BUG,
    _ADD_REST_ENDPOINT,
    _ADD_DB_MIGRATION,
    _FIX_FAILING_TEST,
    _OPTIMIZE_QUERY,
    _FIND_SECURITY_ISSUE,
]

_BY_ID = {task.id: task for task in BENCHMARK_TASKS}

_ALPHANUMERIC = re.compile(r"^[A-Za-z0-9_-]+$")


def task_by_id(task_id: str) -> EvalTask:
    """Return the benchmark with ``task_id``, or raise ``KeyError``."""
    return _BY_ID[task_id]


def task_ids() -> list[str]:
    """Return the registered task ids, in registry order."""
    return list(_BY_ID)


def scaffold(task: EvalTask, workspace_dir: Path) -> Path:
    """Write ``task.files`` into ``workspace_dir`` and return the path.

    The directory is created if needed, and every file is confined to the
    workspace root so task definitions cannot escape it. No git repository is
    created here; callers that need one do it explicitly.
    """
    workspace_dir.mkdir(parents=True, exist_ok=True)
    for relative, content in task.files.items():
        try:
            path = resolve_within(workspace_dir, relative)
        except PathTraversalError as exc:
            raise ValueError(f"task {task.id} file escapes workspace: {relative}") from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return workspace_dir


def validate_task_id(task_id: str) -> bool:
    """Return True when ``task_id`` is a well-formed benchmark identifier."""
    return _ALPHANUMERIC.fullmatch(task_id) is not None
