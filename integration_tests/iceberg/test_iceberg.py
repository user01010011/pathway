import json
import os
import pickle
import threading
import time
import uuid

import pandas as pd
import pytest
from dateutil import tz
from pyiceberg.catalog import load_catalog

import pathway as pw
from pathway.internals.parse_graph import G
from pathway.tests.utils import run, wait_result_with_checker

INPUT_CONTENTS_1 = """{"user_id": 1, "name": "John"}
{"user_id": 2, "name": "Jane"}
{"user_id": 3, "name": "Alice"}
{"user_id": 4, "name": "Bob"}"""
INPUT_CONTENTS_2 = """{"user_id": 5, "name": "Peter"}
{"user_id": 6, "name": "Jake"}
{"user_id": 7, "name": "Dora"}
{"user_id": 8, "name": "Barbara"}"""
INPUT_CONTENTS_3 = """{"user_id": 9, "name": "Anna"}
{"user_id": 10, "name": "Paul"}
{"user_id": 11, "name": "Steve"}
{"user_id": 12, "name": "Sarah"}"""

CATALOG_URI = os.environ.get("ICEBERG_CATALOG_URI", "http://iceberg:8181")
INPUT_CONTENTS = {
    1: INPUT_CONTENTS_1,
    2: INPUT_CONTENTS_2,
    3: INPUT_CONTENTS_3,
}


def _get_pandas_table(table_name: str) -> pd.DataFrame:
    catalog = load_catalog(name="default", uri=CATALOG_URI)
    table = catalog.load_table(table_name)
    scan = table.scan()
    return scan.to_pandas()


class IcebergEntriesCountChecker:
    def __init__(self, table_name: str, expected_count: int):
        self.table_name = table_name
        self.expected_count = expected_count

    def __call__(self):
        try:
            table = _get_pandas_table(self.table_name)
            return len(table) == self.expected_count
        except Exception:
            return False


def test_iceberg_read_after_write(tmp_path):
    input_path = tmp_path / "input.txt"
    output_path = tmp_path / "output.txt"
    pstorage_path = tmp_path / "pstorage"
    table_name = str(uuid.uuid4())

    def run_single_iteration(seq_number: int):
        input_path.write_text(INPUT_CONTENTS[seq_number])
        name_for_id_from_new_part = {}
        for input_line in INPUT_CONTENTS[seq_number].splitlines():
            data = json.loads(input_line)
            name_for_id_from_new_part[data["user_id"]] = data["name"]

        class InputSchema(pw.Schema):
            user_id: int = pw.column_definition(primary_key=True)
            name: str

        # Place some data into the Iceberg table
        table = pw.io.jsonlines.read(
            input_path,
            schema=InputSchema,
            mode="static",
        )
        pw.io.iceberg.write(
            table,
            catalog_uri=CATALOG_URI,
            namespace=["my_database"],
            table_name=table_name,
        )
        run()

        # Read the data from the Iceberg table via Pathway
        G.clear()
        table = pw.io.iceberg.read(
            catalog_uri=CATALOG_URI,
            namespace=["my_database"],
            table_name=table_name,
            mode="static",
            schema=InputSchema,
        )
        pw.io.jsonlines.write(table, output_path)
        persistence_config = pw.persistence.Config(
            pw.persistence.Backend.filesystem(pstorage_path),
        )
        run(persistence_config=persistence_config)

        name_for_id = {}
        with open(output_path, "r") as f:
            for line in f:
                data = json.loads(line)
                name_for_id[data["user_id"]] = data["name"]
        assert name_for_id == name_for_id_from_new_part

    for seq_number in range(1, len(INPUT_CONTENTS) + 1):
        run_single_iteration(seq_number)


@pytest.mark.flaky(reruns=5)
def test_iceberg_several_runs(tmp_path):
    input_path = tmp_path / "input.txt"
    table_name = str(uuid.uuid4())
    all_ids = set()
    all_names = set()

    def run_single_iteration(seq_number: int):
        input_path.write_text(INPUT_CONTENTS[seq_number])
        for input_line in INPUT_CONTENTS[seq_number].splitlines():
            data = json.loads(input_line)
            all_ids.add(data["user_id"])
            all_names.add(data["name"])

        class InputSchema(pw.Schema):
            user_id: int
            name: str

        G.clear()
        table = pw.io.jsonlines.read(
            input_path,
            schema=InputSchema,
            mode="static",
        )
        pw.io.iceberg.write(
            table,
            catalog_uri=CATALOG_URI,
            namespace=["my_database"],
            table_name=table_name,
        )
        run()

        iceberg_table_name = f"my_database.{table_name}"
        pandas_table = _get_pandas_table(iceberg_table_name)
        assert pandas_table.shape == (4 * seq_number, 4)
        assert set(pandas_table["user_id"]) == all_ids
        assert set(pandas_table["name"]) == all_names
        assert set(pandas_table["diff"]) == {1}
        assert len(set(pandas_table["time"])) == seq_number

    # The first run includes the table creation.
    # The second and the following runs check that the case with the created table also works correctly.
    for seq_number in range(1, len(INPUT_CONTENTS) + 1):
        run_single_iteration(seq_number)


def test_iceberg_streaming(tmp_path):
    inputs_path = tmp_path / "inputs"
    inputs_path.mkdir()
    table_name = str(uuid.uuid4())

    def stream_inputs():
        for i in range(1, len(INPUT_CONTENTS) + 1):
            input_path = inputs_path / f"{i}.txt"
            input_path.write_text(INPUT_CONTENTS[i])
            time.sleep(5)

    class InputSchema(pw.Schema):
        user_id: int
        name: str

    table = pw.io.jsonlines.read(
        inputs_path,
        schema=InputSchema,
        mode="streaming",
    )
    pw.io.iceberg.write(
        table,
        catalog_uri=CATALOG_URI,
        namespace=["my_database"],
        table_name=table_name,
        min_commit_frequency=1000,
    )

    t = threading.Thread(target=stream_inputs)
    t.start()
    wait_result_with_checker(
        IcebergEntriesCountChecker(
            f"my_database.{table_name}", 4 * len(INPUT_CONTENTS)
        ),
        30,
    )

    all_ids = set()
    all_names = set()
    for i in range(1, len(INPUT_CONTENTS) + 1):
        for input_line in INPUT_CONTENTS[i].splitlines():
            data = json.loads(input_line)
            all_ids.add(data["user_id"])
            all_names.add(data["name"])

    iceberg_table_name = f"my_database.{table_name}"
    pandas_table = _get_pandas_table(iceberg_table_name)
    assert pandas_table.shape == (4 * len(INPUT_CONTENTS), 4)
    assert set(pandas_table["user_id"]) == all_ids
    assert set(pandas_table["name"]) == all_names
    assert set(pandas_table["diff"]) == {1}
    assert len(set(pandas_table["time"])) == len(INPUT_CONTENTS)


def test_py_object_wrapper_in_iceberg(tmp_path):
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    iceberg_table_name = str(uuid.uuid4())
    input_path.write_text("test")

    table = pw.io.plaintext.read(input_path, mode="static")
    table = table.select(
        data=pw.this.data,
        fun=pw.wrap_py_object(len, serializer=pickle),  # type: ignore
    )
    pw.io.iceberg.write(
        table,
        catalog_uri=CATALOG_URI,
        namespace=["my_database"],
        table_name=iceberg_table_name,
    )
    run()
    G.clear()

    class InputSchema(pw.Schema):
        data: str = pw.column_definition(primary_key=True)
        fun: pw.PyObjectWrapper

    @pw.udf
    def use_python_object(a: pw.PyObjectWrapper, x: str) -> int:
        return a.value(x)

    table = pw.io.iceberg.read(
        catalog_uri=CATALOG_URI,
        namespace=["my_database"],
        table_name=iceberg_table_name,
        mode="static",
        schema=InputSchema,
    )
    table = table.select(len=use_python_object(pw.this.fun, pw.this.data))
    pw.io.jsonlines.write(table, output_path)
    run()

    with open(output_path, "r") as f:
        data = json.load(f)
        assert data["len"] == 4


def test_iceberg_different_types_serialization(tmp_path):
    input_path = tmp_path / "input.jsonl"
    iceberg_table_name = str(uuid.uuid4())
    input_path.write_text("test")

    column_values = {
        "boolean": True,
        "integer": 123,
        "double": -5.6,
        "string": "abcdef",
        "binary_data": b"fedcba",
        "datetime_naive": pw.DateTimeNaive(year=2025, month=1, day=17),
        "datetime_utc_aware": pw.DateTimeUtc(year=2025, month=1, day=17, tz=tz.UTC),
        "duration": pw.Duration(days=5),
        "json_data": pw.Json.parse('{"a": 15, "b": "hello"}'),
    }
    table = pw.io.plaintext.read(input_path, mode="static")
    table = table.select(
        data=pw.this.data,
        **column_values,
    )
    pw.io.iceberg.write(
        table,
        catalog_uri=CATALOG_URI,
        namespace=["my_database"],
        table_name=iceberg_table_name,
    )
    run()
    G.clear()

    class InputSchema(pw.Schema):
        data: str = pw.column_definition(primary_key=True)
        boolean: bool
        integer: int
        double: float
        string: str
        binary_data: bytes
        datetime_naive: pw.DateTimeNaive
        datetime_utc_aware: pw.DateTimeUtc
        duration: pw.Duration
        json_data: pw.Json

    def on_change(key, row, time, is_addition):
        for field, expected_value in column_values.items():
            assert row[field] == expected_value

    table = pw.io.iceberg.read(
        catalog_uri=CATALOG_URI,
        namespace=["my_database"],
        table_name=iceberg_table_name,
        mode="static",
        schema=InputSchema,
    )
    pw.io.subscribe(table, on_change=on_change)
    run()
