from databricks.labs.dqx.profiler.dlt_generator import generate_dlt_rules
from databricks.labs.dqx.profiler.profiler import DQProfile

test_rules = [
    DQProfile(
        name="is_not_null", column="vendor_id", description="Column vendor_id has 0.3% of null values (allowed 1.0%)"
    ),
    DQProfile(name="is_in", column="vendor_id", parameters={"in": ["1", "4", "2"]}),
    DQProfile(name="is_not_null_or_empty", column="vendor_id", parameters={"trim_strings": True}),
    DQProfile(
        name="min_max",
        column="rate_code_id",
        parameters={"min": 1, "max": 265},
        description="Real min/max values were used",
    ),
]


def test_generate_dlt_sql_expect():
    expectations = generate_dlt_rules(test_rules)
    expected = [
        "CONSTRAINT vendor_id_is_not_null EXPECT (vendor_id is not null)",
        "CONSTRAINT vendor_id_is_in EXPECT (vendor_id in ('1', '4', '2'))",
        "CONSTRAINT vendor_id_is_not_null_or_empty EXPECT (vendor_id is not null and trim(vendor_id) <> '')",
        "CONSTRAINT rate_code_id_min_max EXPECT (rate_code_id >= 1 and rate_code_id <= 265)",
    ]
    assert expectations == expected


def test_generate_dlt_sql_drop():
    expectations = generate_dlt_rules(test_rules, action="drop")
    expected = [
        "CONSTRAINT vendor_id_is_not_null EXPECT (vendor_id is not null) ON VIOLATION DROP ROW",
        "CONSTRAINT vendor_id_is_in EXPECT (vendor_id in ('1', '4', '2')) ON VIOLATION DROP ROW",
        "CONSTRAINT vendor_id_is_not_null_or_empty EXPECT (vendor_id is not null and trim(vendor_id) <> '') ON VIOLATION DROP ROW",
        "CONSTRAINT rate_code_id_min_max EXPECT (rate_code_id >= 1 and rate_code_id <= 265) ON VIOLATION DROP ROW",
    ]
    assert expectations == expected


def test_generate_dlt_sql_fail():
    expectations = generate_dlt_rules(test_rules, action="fail")
    expected = [
        "CONSTRAINT vendor_id_is_not_null EXPECT (vendor_id is not null) ON VIOLATION FAIL UPDATE",
        "CONSTRAINT vendor_id_is_in EXPECT (vendor_id in ('1', '4', '2')) ON VIOLATION FAIL UPDATE",
        "CONSTRAINT vendor_id_is_not_null_or_empty EXPECT (vendor_id is not null and trim(vendor_id) <> '') ON VIOLATION FAIL UPDATE",
        "CONSTRAINT rate_code_id_min_max EXPECT (rate_code_id >= 1 and rate_code_id <= 265) ON VIOLATION FAIL UPDATE",
    ]
    assert expectations == expected


def test_generate_dlt_python_expect():
    expectations = generate_dlt_rules(test_rules, language="Python")
    expected = """@dlt.expect_all(
{"vendor_id_is_not_null": "vendor_id is not null", "vendor_id_is_in": "vendor_id in ('1', '4', '2')", "vendor_id_is_not_null_or_empty": "vendor_id is not null and trim(vendor_id) <> ''", "rate_code_id_min_max": "rate_code_id >= 1 and rate_code_id <= 265"}
)"""
    assert expectations == expected


def test_generate_dlt_python_drop():
    expectations = generate_dlt_rules(test_rules, language="Python", action="drop")
    expected = """@dlt.expect_all_or_drop(
{"vendor_id_is_not_null": "vendor_id is not null", "vendor_id_is_in": "vendor_id in ('1', '4', '2')", "vendor_id_is_not_null_or_empty": "vendor_id is not null and trim(vendor_id) <> ''", "rate_code_id_min_max": "rate_code_id >= 1 and rate_code_id <= 265"}
)"""
    assert expectations == expected


def test_generate_dlt_python_fail():
    expectations = generate_dlt_rules(test_rules, language="Python", action="fail")
    expected = """@dlt.expect_all_or_fail(
{"vendor_id_is_not_null": "vendor_id is not null", "vendor_id_is_in": "vendor_id in ('1', '4', '2')", "vendor_id_is_not_null_or_empty": "vendor_id is not null and trim(vendor_id) <> ''", "rate_code_id_min_max": "rate_code_id >= 1 and rate_code_id <= 265"}
)"""
    assert expectations == expected
