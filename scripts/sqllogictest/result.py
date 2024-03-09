from hashlib import md5

from .base_statement import BaseStatement
from .statement import (
    Statement,
    Require,
    Mode,
    Halt,
    Set,
    Load,
    Query,
    HashThreshold,
    Loop,
    Foreach,
    Endloop,
    RequireEnv,
    Restart,
    Reconnect,
    Sleep,
    SleepUnit,
    Skip,
    SortStyle,
    Unskip,
)

from .expected_result import ExpectedResult
from .parser import SQLLogicTest
from typing import Optional, Any, Tuple, List, Dict, Set, Generator
from .logger import SQLLogicTestLogger
import duckdb
import os
import math
import time

import re
from functools import cmp_to_key
from enum import Enum


class QueryResult:
    def __init__(self, result: List[Tuple[Any]], types: List[str], error: Optional[Exception] = None):
        self._result = result
        self.types = types
        self.error = error
        if not error:
            self._column_count = len(self.types)
            self._row_count = len(result)

    def get_value(self, column, row):
        return self._result[row][column]

    def row_count(self) -> int:
        return self._row_count

    def column_count(self) -> int:
        assert self._column_count != 0
        return self._column_count

    def has_error(self) -> bool:
        return self.error != None

    def get_error(self) -> Optional[Exception]:
        return self.error


def compare_values(result: QueryResult, actual_str, expected_str, current_column):
    error = False

    if actual_str == expected_str:
        return True

    if expected_str.startswith("<REGEX>:") or expected_str.startswith("<!REGEX>:"):
        if expected_str.startswith("<REGEX>:"):
            should_match = True
            regex_str = expected_str.replace("<REGEX>:", "")
        else:
            should_match = False
            regex_str = expected_str.replace("<!REGEX>:", "")
        re_options = re.DOTALL
        re_pattern = re.compile(regex_str, re_options)
        regex_matches = bool(re_pattern.fullmatch(actual_str))
        if regex_matches == should_match:
            return True

    sql_type = result.types[current_column]

    def is_numeric(type) -> bool:
        NUMERIC_TYPES = [
            "TINYINT",
            "SMALLINT",
            "INTEGER",
            "BIGINT",
            "HUGEINT",
            "FLOAT",
            "DOUBLE",
            "DECIMAL",
            "UTINYINT",
            "USMALLINT",
            "UINTEGER",
            "UBIGINT",
            "UHUGEINT",
        ]
        if str(type) in NUMERIC_TYPES:
            return True
        return 'DECIMAL' in str(type)

    if is_numeric(sql_type):
        if sql_type in [duckdb.typing.FLOAT, duckdb.typing.DOUBLE]:
            # ApproxEqual
            expected = convert_value(expected_str, sql_type)
            actual = convert_value(actual_str, sql_type)
            if expected == actual:
                return True
            if math.isnan(expected) and math.isnan(actual):
                return True
            epsilon = abs(actual) * 0.01 + 0.00000001
            if abs(expected - actual) <= epsilon:
                return True
            return False
        expected = convert_value(expected_str, sql_type)
        actual = convert_value(actual_str, sql_type)
        return expected == actual

    if sql_type == duckdb.typing.BOOLEAN or sql_type.id == 'timestamp with time zone':
        expected = convert_value(expected_str, sql_type)
        actual = convert_value(actual_str, sql_type)
        return expected == actual
    expected = sql_logic_test_convert_value(expected_str, sql_type, False)
    actual = actual_str
    error = actual != expected

    if error:
        return False
    return True


def result_is_hash(result):
    parts = result.split()
    if len(parts) != 5:
        return False
    if not parts[0].isdigit():
        return False
    if parts[1] != "values" or parts[2] != "hashing" or len(parts[4]) != 32:
        return False
    return all([x.islower() or x.isnumeric() for x in parts[4]])


def result_is_file(result: str):
    return result.startswith('<FILE>:')


def load_result_from_file(fname, names):
    con = duckdb.connect()
    con.execute(f"PRAGMA threads={os.cpu_count()}")

    fname = fname.replace("<FILE>:", "")

    struct_definition = "STRUCT_PACK("
    for i in range(len(names)):
        if i > 0:
            struct_definition += ", "
        struct_definition += f"{names[i]} := VARCHAR"
    struct_definition += ")"

    csv_result = con.execute(
        f"""
        SELECT * FROM read_csv(
            '{fname}',
            header=1,
            sep='|',
            columns={struct_definition},
            auto_detect=false,
            all_varchar=true
        )
    """
    )

    return csv_result.fetchall()


def convert_value(value, type: str):
    if value is None or value == 'NULL':
        return 'NULL'
    query = f'select $1::{type}'
    return duckdb.execute(query, [value]).fetchone()[0]


def sql_logic_test_convert_value(value, sql_type, is_sqlite_test: bool) -> str:
    if value is None or value == 'NULL':
        return 'NULL'
    if is_sqlite_test:
        if sql_type in [
            duckdb.typing.BOOLEAN,
            duckdb.typing.DOUBLE,
            duckdb.typing.FLOAT,
        ] or any([type_str in str(sql_type) for type_str in ['DECIMAL', 'HUGEINT']]):
            return convert_value(value, 'BIGINT::VARCHAR')
    if sql_type == duckdb.typing.BOOLEAN:
        return "1" if convert_value(value, sql_type) else "0"
    else:
        res = convert_value(value, 'VARCHAR')
        if len(res) == 0:
            res = "(empty)"
        else:
            res = res.replace("\0", "\\0")
    return res


def duck_db_convert_result(result: QueryResult, is_sqlite_test: bool) -> List[str]:
    out_result = []
    row_count = result.row_count()
    column_count = result.column_count()

    for r in range(row_count):
        for c in range(column_count):
            value = result.get_value(c, r)
            converted_value = sql_logic_test_convert_value(value, result.types[c], is_sqlite_test)
            out_result.append(converted_value)

    return out_result


class RequireResult(Enum):
    MISSING = 0
    PRESENT = 1


class SkipException(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)


class ExecuteResult:
    class Type(Enum):
        SUCCES = 0
        ERROR = 1
        SKIPPED = 2

    def __init__(self, type: "ExecuteResult.Type"):
        self.type = type


class SQLLogicRunner:
    __slots__ = [
        'skipped',
        'error',
        'skip_level',
        'dbpath',
        'loaded_databases',
        'db',
        'config',
        'extensions',
        'con',
        'cursors',
        'environment_variables',
        'test',
        'hash_threshold',
        'hash_label_map',
        'result_label_map',
        'required_requires',
        'output_hash_mode',
        'output_result_mode',
        'debug_mode',
        'finished_processing_file',
        'ignore_error_messages',
        'always_fail_error_messages',
        'original_sqlite_test',
    ]

    def reset(self):
        self.skipped = False
        self.error: Optional[str] = None
        self.skip_level: int = 0

        self.dbpath = ''
        self.loaded_databases: Set[str] = set()
        self.db: Optional[duckdb.DuckDBPyConnection] = None
        self.config: Dict[str, Any] = {
            'allow_unsigned_extensions': True,
            'allow_unredacted_secrets': True,
        }
        self.extensions: set = set()

        self.con: Optional[duckdb.DuckDBPyConnection] = None
        self.cursors: Dict[str, duckdb.DuckDBPyConnection] = {}

        self.environment_variables: Dict[str, str] = {}
        self.test: Optional[SQLLogicTest] = None

        self.hash_threshold: int = 0
        self.hash_label_map: Dict[str, str] = {}
        self.result_label_map: Dict[str, Any] = {}

        self.required_requires: set = set()
        self.output_hash_mode = False
        self.output_result_mode = False
        self.debug_mode = False

        self.finished_processing_file = False
        # If these error messages occur in a test, the test will abort but still count as passed
        self.ignore_error_messages = {"HTTP", "Unable to connect"}
        # If these error messages occur in a statement that is expected to fail, the test will fail
        self.always_fail_error_messages = {"differs from original result!", "INTERNAL"}

        self.original_sqlite_test = False

    def skip_error_message(self, message):
        for error_message in self.ignore_error_messages:
            if error_message in str(message):
                return True
        return False

    def __init__(self):
        self.reset()

    def fail_query(self, query: Query):
        # if context.is_parallel:
        #    self.finished_processing_file = True
        #    context.error_file = file_name
        #    context.error_line = query_line
        # else:
        #    fail_line(file_name, query_line, 0)
        self.fail(f'Failed: {self.test.path}:{query.get_query_line()}')

    def fail(self, message):
        raise Exception(message)

    def skip(self):
        self.skip_level += 1

    def skiptest(self, message: str):
        raise SkipException(message)

    def unskip(self):
        self.skip_level -= 1

    def skip_active(self) -> bool:
        return self.skip_level > 0

    def is_required(self, param):
        return param in self.required_requires

    def load_extension(self, db: duckdb.DuckDBPyConnection, extension: str):
        root = duckdb.__build_dir__
        path = os.path.join(root, "extension", extension, f"{extension}.duckdb_extension")
        # Serialize it as a POSIX compliant path
        query = f"LOAD '{path}'"
        db.execute(query)

    def check_require(self, statement: Require) -> RequireResult:
        not_an_extension = [
            "notmingw",
            "mingw",
            "notwindows",
            "windows",
            "longdouble",
            "64bit",
            "noforcestorage",
            "nothreadsan",
            "strinline",
            "vector_size",
            "exact_vector_size",
            "block_size",
            "skip_reload",
            "noalternativeverify",
        ]
        param = statement.header.parameters[0].lower()
        if param in not_an_extension:
            return RequireResult.MISSING

        if param == "no_extension_autoloading":
            if 'autoload_known_extensions' in self.config:
                # If autoloading is on, we skip this test
                return RequireResult.MISSING
            return RequireResult.PRESENT

        excluded_from_autoloading = True
        for ext in self.AUTOLOADABLE_EXTENSIONS:
            if ext == param:
                excluded_from_autoloading = False
                break

        if not 'autoload_known_extensions' in self.config:
            try:
                self.load_extension(self.con, param)
                self.extensions.add(param)
            except:
                return RequireResult.MISSING
        elif excluded_from_autoloading:
            return RequireResult.MISSING

        return RequireResult.PRESENT

    def load_database(self, dbpath):
        self.dbpath = dbpath

        # Restart the database with the specified db path
        self.db = None
        self.con = None
        self.cursors = {}

        # Now re-open the current database
        read_only = 'access_mode' in self.config and self.config['access_mode'] == 'read_only'
        self.db = duckdb.connect(dbpath, read_only, self.config)
        self.loaded_databases.add(dbpath)
        self.reconnect()

        # Load any previously loaded extensions again
        for extension in self.extensions:
            self.load_extension(self.db, extension)

    def check_query_result(self, context, query: Query, result: QueryResult) -> None:
        expected_column_count = query.expected_result.get_expected_column_count()
        values = query.expected_result.lines
        sort_style = query.get_sortstyle()
        query_label = query.get_label()
        query_has_label = query_label != None

        logger = SQLLogicTestLogger(context, query, self.test.path)

        # If the result has an error, log it
        if result.has_error():
            logger.unexpected_failure(result)
            if self.skip_error_message(result.get_error()):
                self.finished_processing_file = True
                return
            print(result.get_error())
            self.fail_query(query)

        row_count = result.row_count()
        column_count = result.column_count()
        total_value_count = row_count * column_count

        if len(values) == 1 and result_is_hash(values[0]):
            compare_hash = True
            is_hash = True
        else:
            compare_hash = query_has_label or (self.hash_threshold > 0 and total_value_count > self.hash_threshold)
            is_hash = False

        result_values_string = duck_db_convert_result(result, self.original_sqlite_test)

        if self.output_result_mode:
            logger.output_result(result, result_values_string)

        if sort_style == SortStyle.ROW_SORT:
            ncols = result.column_count()
            nrows = int(total_value_count / ncols)
            rows = [result_values_string[i * ncols : (i + 1) * ncols] for i in range(nrows)]

            # Define the comparison function
            def compare_rows(a, b):
                for col_idx, val in enumerate(a):
                    a_val = val
                    b_val = b[col_idx]
                    if a_val != b_val:
                        return -1 if a_val < b_val else 1
                return 0

            # Sort the individual rows based on element comparison
            sorted_rows = sorted(rows, key=cmp_to_key(compare_rows))
            rows = sorted_rows

            for row_idx, row in enumerate(rows):
                for col_idx, val in enumerate(row):
                    result_values_string[row_idx * ncols + col_idx] = val
        elif sort_style == SortStyle.VALUE_SORT:
            result_values_string.sort()

        comparison_values = []
        if len(values) == 1 and result_is_file(values[0]):
            fname = context.replace_keywords(values[0])
            csv_error = ""
            comparison_values = load_result_from_file(fname, result.names, expected_column_count, csv_error)
            if csv_error:
                logger.print_error_header(csv_error)
                self.fail_query(query)
        else:
            comparison_values = values

        hash_value = ""
        if self.output_hash_mode or compare_hash:
            hash_context = md5()
            for val in result_values_string:
                hash_context.update(str(val).encode())
                hash_context.update("\n".encode())
            digest = hash_context.hexdigest()
            hash_value = f"{total_value_count} values hashing to {digest}"
            if self.output_hash_mode:
                logger.output_hash(hash_value)
                return

        if not compare_hash:
            original_expected_columns = expected_column_count
            column_count_mismatch = False

            if expected_column_count != result.column_count():
                expected_column_count = result.column_count()
                column_count_mismatch = True

            expected_rows = len(comparison_values) / expected_column_count
            row_wise = expected_column_count > 1 and len(comparison_values) == result.row_count()

            if not row_wise:
                all_tabs = all("\t" in val for val in comparison_values)
                row_wise = all_tabs

            if row_wise:
                expected_rows = len(comparison_values)
                row_wise = True
            elif len(comparison_values) % expected_column_count != 0:
                if column_count_mismatch:
                    logger.column_count_mismatch(result, query.values, original_expected_columns, row_wise)
                else:
                    logger.not_cleanly_divisible(expected_column_count, len(comparison_values))
                self.fail_query(query)

            if expected_rows != result.row_count():
                if column_count_mismatch:
                    logger.column_count_mismatch(result, query.values, original_expected_columns, row_wise)
                else:
                    logger.wrong_row_count(
                        expected_rows, result_values_string, comparison_values, expected_column_count, row_wise
                    )
                self.fail_query(query)

            if row_wise:
                current_row = 0
                for i, val in enumerate(comparison_values):
                    splits = [x for x in val.split("\t") if x != '']
                    if len(splits) != expected_column_count:
                        if column_count_mismatch:
                            logger.column_count_mismatch(result, query.values, original_expected_columns, row_wise)
                        logger.split_mismatch(i + 1, expected_column_count, len(splits))
                        self.fail_query(query)
                    for c, split_val in enumerate(splits):
                        lvalue_str = result_values_string[current_row * expected_column_count + c]
                        rvalue_str = split_val
                        success = compare_values(result, lvalue_str, split_val, c)
                        if not success:
                            logger.print_error_header("Wrong result in query!")
                            logger.print_line_sep()
                            logger.print_sql()
                            logger.print_line_sep()
                            print(f"Mismatch on row {current_row + 1}, column {c + 1}")
                            print(f"{lvalue_str} <> {rvalue_str}")
                            logger.print_line_sep()
                            logger.print_result_error(result_values_string, values, expected_column_count, row_wise)
                            self.fail_query(query)
                        # Increment the assertion counter
                        assert success
                    current_row += 1
            else:
                current_row, current_column = 0, 0
                for i, val in enumerate(comparison_values):
                    lvalue_str = result_values_string[current_row * expected_column_count + current_column]
                    rvalue_str = val
                    success = compare_values(result, lvalue_str, rvalue_str, current_column)
                    if not success:
                        logger.print_error_header("Wrong result in query!")
                        logger.print_line_sep()
                        logger.print_sql()
                        logger.print_line_sep()
                        print(f"Mismatch on row {current_row + 1}, column {current_column + 1}")
                        print(f"{lvalue_str} <> {rvalue_str}")
                        logger.print_line_sep()
                        logger.print_result_error(result_values_string, values, expected_column_count, row_wise)
                        self.fail_query(query)
                    # Increment the assertion counter
                    assert success

                    current_column += 1
                    if current_column == expected_column_count:
                        current_row += 1
                        current_column = 0

            if column_count_mismatch:
                logger.column_count_mismatch_correct_result(original_expected_columns, expected_column_count, result)
                self.fail_query(query)
        else:
            hash_compare_error = False
            if query_has_label:
                entry = self.hash_label_map.get(query_label)
                if entry is None:
                    self.hash_label_map[query_label] = hash_value
                    self.result_label_map[query_label] = result
                else:
                    hash_compare_error = entry != hash_value

            if is_hash:
                hash_compare_error = values[0] != hash_value

            if hash_compare_error:
                expected_result = self.result_label_map.get(query_label)
                logger.wrong_result_hash(expected_result, result)
                self.fail_query(query)

            assert not hash_compare_error


class SQLLogicContext:
    __slots__ = ['iterator', 'runner', 'generator', 'STATEMENTS', 'statements', 'keywords']

    def reset(self):
        self.iterator = 0

    def replace_keywords(self, input: str):
        # Apply a replacement for every registered keyword
        for key, value in self.keywords.items():
            input = input.replace(key, value)
        return input

    def __init__(
        self, runner: SQLLogicRunner, statements: List[BaseStatement], keywords: Dict[str, str], iteration_generator
    ):
        self.statements = statements
        self.runner = runner
        self.generator: Generator[Any] = iteration_generator
        self.keywords = keywords
        self.STATEMENTS = {
            Query: self.execute_query,
            Statement: self.execute_statement,
            RequireEnv: self.execute_require_env,
            Require: self.execute_require,
            Load: self.execute_load,
            Skip: self.execute_skip,
            Unskip: self.execute_unskip,
            Mode: self.execute_mode,
            Sleep: self.execute_sleep,
            Reconnect: self.execute_reconnect,
            Halt: self.execute_halt,
            Restart: self.execute_restart,
            HashThreshold: self.execute_hash_threshold,
            Set: self.execute_set,
            Loop: self.execute_loop,
            Foreach: self.execute_foreach,
            Endloop: None,  # <-- should never be encountered outside of Loop/Foreach
        }

    def in_loop(self) -> bool:
        # FIXME: support loops
        return False

    def execute_load(self, load: Load):
        if self.in_loop():
            self.runner.fail("load cannot be called in a loop")

        readonly = load.readonly

        if load.header.parameters:
            dbpath = load.header.parameters[0]
            dbpath = self.replace_keywords(dbpath)
            if not readonly:
                # delete the target database file, if it exists
                self.runner.delete_database(dbpath)
        else:
            dbpath = ""

        # set up the config file
        if readonly:
            self.runner.config['temp_directory'] = False
            self.runner.config['access_mode'] = 'read_only'
        else:
            self.runner.config['temp_directory'] = True
            self.runner.config['access_mode'] = 'automatic'

        # now create the database file
        self.runner.load_database(dbpath)

    def execute_query(self, query: Query):
        assert isinstance(query, Query)
        conn = self.runner.get_connection(query.connection_name)
        sql_query = '\n'.join(query.lines)
        sql_query = self.replace_keywords(sql_query)

        expected_result = query.expected_result
        assert expected_result.type == ExpectedResult.Type.SUCCES

        try:
            statements = conn.extract_statements(sql_query)
            statement = statements[-1]
            if 'pivot' in sql_query and len(statements) != 1:
                self.runner.skiptest("Can not deal properly with a PIVOT statement")

            def is_query_result(sql_query, statement) -> bool:
                if duckdb.ExpectedResultType.QUERY_RESULT not in statement.expected_result_type:
                    return False
                if statement.type in [
                    duckdb.StatementType.DELETE,
                    duckdb.StatementType.UPDATE,
                    duckdb.StatementType.INSERT,
                ]:
                    if 'returning' not in sql_query.lower():
                        return False
                    return True
                return len(statement.expected_result_type) == 1

            if is_query_result(sql_query, statement):
                original_rel = conn.query(sql_query)
                original_types = original_rel.types
                # We create new names for the columns, because they might be duplicated
                aliased_columns = [f'c{i}' for i in range(len(original_types))]

                expressions = [f'"{name}"::VARCHAR' for name, sql_type in zip(aliased_columns, original_types)]
                aliased_table = ", ".join(aliased_columns)
                expression_list = ", ".join(expressions)
                try:
                    # Select from the result, converting the Values to the right type for comparison
                    transformed_query = (
                        f"select {expression_list} from original_rel unnamed_subquery_blabla({aliased_table})"
                    )
                    stringified_rel = conn.query(transformed_query)
                except Exception as e:
                    self.fail(f"Could not select from the ValueRelation: {str(e)}")
                result = stringified_rel.fetchall()
                query_result = QueryResult(result, original_types)
            elif duckdb.ExpectedResultType.CHANGED_ROWS in statement.expected_result_type:
                conn.execute(sql_query)
                result = conn.fetchall()
                query_result = QueryResult(result, [duckdb.typing.BIGINT])
            else:
                conn.execute(sql_query)
                result = conn.fetchall()
                query_result = QueryResult(result, [])
            if expected_result.lines == None:
                return
        except SkipException as e:
            self.runner.skipped = True
            return
        except Exception as e:
            print(e)
            query_result = QueryResult([], [], e)

        self.runner.check_query_result(self, query, query_result)

    def execute_skip(self, statement: Skip):
        self.runner.skip()

    def execute_unskip(self, statement: Unskip):
        self.runner.unskip()

    def execute_halt(self, statement: Halt):
        self.runner.skiptest("HALT was encountered in file")

    def execute_restart(self, statement: Restart):
        # if context.is_parallel:
        #    raise RuntimeError("Cannot restart database in parallel")

        old_settings = self.runner.con.execute(
            "select name, value from duckdb_settings() where scope='LOCAL' and value != 'NULL' and value != ''"
        ).fetchall()
        existing_search_path = self.runner.con.execute("select current_setting('search_path')").fetchone()[0]

        self.runner.load_database(self.runner.dbpath)
        for setting in old_settings:
            name, value = setting
            query = f"set {name}='{value}'"
            print(query)
            self.runner.con.execute(query)

        self.runner.con.begin()
        self.runner.con.execute(f"set search_path = '{existing_search_path}'")
        self.runner.con.commit()

    def execute_set(self, statement: Set):
        option = statement.header.parameters[0]
        string_set = (
            self.runner.ignore_error_messages
            if option == "ignore_error_messages"
            else self.runner.always_fail_error_messages
        )
        string_set.clear()
        string_set = statement.error_messages

    def execute_hash_threshold(self, statement: HashThreshold):
        self.runner.hash_threshold = statement.threshold

    def execute_reconnect(self, statement: Reconnect):
        # if self.is_parallel:
        #   raise Error(...)
        self.runner.reconnect()

    def execute_sleep(self, statement: Sleep):
        def calculate_sleep_time(duration: float, unit: SleepUnit) -> float:
            if unit == SleepUnit.SECOND:
                return duration
            elif unit == SleepUnit.MILLISECOND:
                return duration / 1000
            elif unit == SleepUnit.MICROSECOND:
                return duration / 1000000
            elif unit == SleepUnit.NANOSECOND:
                return duration / 1000000000
            else:
                raise ValueError("Unknown sleep unit")

        unit = statement.get_unit()
        duration = statement.get_duration()

        time_to_sleep = calculate_sleep_time(duration, unit)
        time.sleep(time_to_sleep)

    def execute_mode(self, statement: Mode):
        parameter = statement.header.parameters[0]
        if parameter == "output_hash":
            self.runner.output_hash_mode = True
        elif parameter == "output_result":
            self.runner.output_result_mode = True
        elif parameter == "no_output":
            self.runner.output_hash_mode = False
            self.runner.output_result_mode = False
        elif parameter == "debug":
            self.runner.debug_mode = True
        else:
            raise RuntimeError("unrecognized mode: " + parameter)

    def execute_statement(self, statement: Statement):
        assert isinstance(statement, Statement)
        conn = self.runner.get_connection(statement.connection_name)
        sql_query = '\n'.join(statement.lines)
        sql_query = self.replace_keywords(sql_query)

        expected_result = statement.expected_result
        try:
            conn.execute(sql_query)
            result = conn.fetchall()
            if expected_result.type == ExpectedResult.Type.ERROR:
                self.runner.fail(f"Query unexpectedly succeeded")
            if expected_result.type != ExpectedResult.Type.UNKNOWN:
                assert expected_result.lines == None
        except duckdb.Error as e:
            if expected_result.type == ExpectedResult.Type.SUCCES:
                self.runner.fail(f"Query unexpectedly failed: {str(e)}")
            if expected_result.lines == None:
                return
            expected = '\n'.join(expected_result.lines)
            # Sanitize the expected error
            if expected.startswith('Dependency Error: '):
                expected = expected.split('Dependency Error: ')[1]
            if expected not in str(e):
                self.runner.fail(
                    f"Query failed, but did not produce the right error: {expected}\nInstead it produced: {str(e)}"
                )

    def execute_require(self, statement: Require):
        require_result = self.runner.check_require(statement)
        if require_result == RequireResult.MISSING:
            param = statement.header.parameters[0].lower()
            if self.runner.is_required(param):
                # This extension / setting was explicitly required
                self.runner.fail("require {}: FAILED".format(param))
            self.runner.skipped = True

    def execute_require_env(self, statement: BaseStatement):
        # TODO: support
        # TODO: add to 'keywords'
        self.runner.skipped = True

    def get_loop_statements(self):
        saved_iterator = self.iterator
        # Loop until EndLoop is found
        statement = None
        depth = 0
        while self.iterator < len(self.statements):
            statement = self.next_statement()
            if statement.__class__ in [Foreach, Loop]:
                depth += 1
            if statement.__class__ == Endloop:
                if depth == 0:
                    break
                depth -= 1
        if not statement or statement.__class__ != Endloop:
            raise Exception("no corresponding 'endloop' found before the end of the file!")
        statements = self.statements[saved_iterator : self.iterator - 1]
        return statements

    def execute_loop(self, loop: Loop):
        if loop.parallel:
            self.runner.skiptest("PARALLEL LOOP NOT SUPPORTED")
        statements = self.get_loop_statements()
        new_keywords = self.keywords

        # Every iteration the 'value' of the loop key needs to change
        def update_value(keywords: Dict[str, str]) -> Generator[Any, Any, Any]:
            loop_key = f'${{{loop.name}}}'
            for val in range(loop.start, loop.end):
                keywords[loop_key] = str(val)
                yield None
            keywords.pop(loop_key)

        loop_context = SQLLogicContext(self.runner, statements, new_keywords, update_value)
        loop_context.execute()

    def execute_foreach(self, foreach: Foreach):
        if foreach.parallel:
            self.runner.skiptest("PARALLEL FOREACH NOT SUPPORTED")
        statements = self.get_loop_statements()
        new_keywords = self.keywords

        # Every iteration the 'value' of the loop key needs to change
        def update_value(keywords: Dict[str, str]) -> Generator[Any, Any, Any]:
            loop_keys = [f'${{{name}}}' for name in foreach.name.split(',')]

            for val in foreach.values:
                if len(loop_keys) != 1:
                    values = val.split(',')
                else:
                    values = [val]
                assert len(values) == len(loop_keys)
                for i, key in enumerate(loop_keys):
                    keywords[key] = str(values[i])
                yield None
            for key in loop_keys:
                keywords.pop(key)

        loop_context = SQLLogicContext(self.runner, statements, new_keywords, update_value)
        loop_context.execute()

    def next_statement(self):
        if self.iterator >= len(self.statements):
            raise Exception("'next_statement' out of range, statements already consumed")
        statement = self.statements[self.iterator]
        self.iterator += 1
        return statement

    def execute(self):
        for _ in self.generator(self.keywords):
            self.reset()
            while self.iterator < len(self.statements):
                statement = self.next_statement()
                if self.runner.skip_active() and statement.__class__ != Unskip:
                    # Keep skipping until Unskip is found
                    continue
                print("Executing:", statement.__class__.__name__, "line:", statement.get_query_line())
                method = self.STATEMENTS.get(statement.__class__)
                if not method:
                    raise Exception(f"Not supported: {statement.__class__.__name__}")
                method(statement)
                if self.runner.skipped:
                    self.runner.skipped = False
                    return ExecuteResult(ExecuteResult.Type.SKIPPED)
        return ExecuteResult(ExecuteResult.Type.SUCCES)
