import os
import functools as ft
import inspect
import itertools
import json
import logging
from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pyspark.sql.functions as F
from pyspark.sql import Column, DataFrame
from databricks.labs.dqx import col_functions
from databricks.labs.blueprint.installation import Installation

from databricks.labs.dqx.base import DQEngineBase
from databricks.labs.dqx.config import WorkspaceConfig
from databricks.labs.dqx.utils import get_column_name
from databricks.sdk.errors import NotFound

logger = logging.getLogger(__name__)


# TODO: make this configurable
class Columns(Enum):
    """Enum class to represent columns in the dataframe that will be used for error and warning reporting."""

    ERRORS = "_errors"
    WARNINGS = "_warnings"


class Criticality(Enum):
    """Enum class to represent criticality of the check."""

    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class ChecksValidationStatus:
    """Class to represent the validation status."""

    _errors: list[str] = field(default_factory=list)

    def add_error(self, error: str):
        """Add an error to the validation status."""
        self._errors.append(error)

    def add_errors(self, errors: list[str]):
        """Add an error to the validation status."""
        self._errors.extend(errors)

    @property
    def has_errors(self) -> bool:
        """Check if there are any errors in the validation status."""
        return bool(self._errors)

    @property
    def errors(self) -> list[str]:
        """Get the list of errors in the validation status."""
        return self._errors

    def to_string(self) -> str:
        """Convert the validation status to a string."""
        if self.has_errors:
            return "\n".join(self._errors)
        return "No errors found"

    def __str__(self) -> str:
        """String representation of the ValidationStatus class."""
        return self.to_string()


@dataclass(frozen=True)
class DQRule:
    """Class to represent a data quality rule consisting of following fields:
    * `check` - Column expression to evaluate. This expression should return string value if it's evaluated to true -
    it will be used as an error/warning message, or `null` if it's evaluated to `false`
    * `name` - optional name that will be given to a resulting column. Autogenerated if not provided
    * `criticality` (optional) - possible values are `error` (critical problems), and `warn` (potential problems)
    """

    check: Column
    name: str = ""
    criticality: str = Criticality.ERROR.value

    def __post_init__(self):
        # take the name from the alias of the column expression if not provided
        object.__setattr__(self, "name", self.name if self.name else "col_" + get_column_name(self.check))

    @ft.cached_property
    def rule_criticality(self) -> str:
        """Returns criticality of the check.

        :return: string describing criticality - `warn` or `error`. Raises exception if it's something else
        """
        criticality = self.criticality
        if criticality not in {Criticality.WARN.value and criticality, Criticality.ERROR.value}:
            criticality = Criticality.ERROR.value

        return criticality

    def check_column(self) -> Column:
        """Creates a Column object from the given check.

        :return: Column object
        """
        return F.when(self.check.isNull(), F.lit(None).cast("string")).otherwise(self.check)


@dataclass(frozen=True)
class DQRuleColSet:
    """Class to represent a data quality col rule set which defines quality check function for a set of columns.
    The class consists of the following fields:
    * `columns` - list of column names to which the given check function should be applied
    * `criticality` - criticality level ('warn' or 'error')
    * `check_func` - check function to be applied
    * `check_func_args` - non-keyword / positional arguments for the check function after the col_name
    * `check_func_kwargs` - keyword /named arguments for the check function after the col_name
    """

    columns: list[str]
    check_func: Callable
    criticality: str = Criticality.ERROR.value
    check_func_args: list[Any] = field(default_factory=list)
    check_func_kwargs: dict[str, Any] = field(default_factory=dict)

    def get_rules(self) -> list[DQRule]:
        """Build a list of rules for a set of columns.

        :return: list of dq rules
        """
        rules = []
        for col_name in self.columns:
            rule = DQRule(
                criticality=self.criticality,
                check=self.check_func(col_name, *self.check_func_args, **self.check_func_kwargs),
            )
            rules.append(rule)
        return rules


class DQEngine(DQEngineBase):
    """Data Quality Engine class to apply data quality checks to a given dataframe."""

    @staticmethod
    def _get_check_columns(checks: list[DQRule], criticality: str) -> list[DQRule]:
        """Get check columns based on criticality.

        :param checks: list of checks to apply to the dataframe
        :param criticality: criticality
        :return: list of check columns
        """
        return [check for check in checks if check.rule_criticality == criticality]

    @staticmethod
    def _append_empty_checks(df: DataFrame) -> DataFrame:
        """Append empty checks at the end of dataframe.

        :param df: dataframe without checks
        :return: dataframe with checks
        """
        return df.select(
            "*",
            F.lit(None).cast("map<string, string>").alias(Columns.ERRORS.value),
            F.lit(None).cast("map<string, string>").alias(Columns.WARNINGS.value),
        )

    @staticmethod
    def _create_results_map(df: DataFrame, checks: list[DQRule], dest_col: str) -> DataFrame:
        """ ""Create a map from the values of the specified columns, using the column names as a key.  This function is
        used to collect individual check columns into corresponding errors and/or warnings columns.

        :param df: dataframe with added check columns
        :param checks: list of checks to apply to the dataframe
        :param dest_col: name of the map column
        """
        empty_type = F.lit(None).cast("map<string, string>").alias(dest_col)
        if len(checks) == 0:
            return df.select("*", empty_type)

        name_cols = []
        check_cols = []
        for check in checks:
            check_cols.append(check.check_column())
            name_cols.append(F.lit(check.name))

        m_col = F.map_from_arrays(F.array(*name_cols), F.array(*check_cols))
        m_col = F.map_filter(m_col, lambda _, v: v.isNotNull())
        return df.withColumn(dest_col, F.when(F.size(m_col) > 0, m_col).otherwise(empty_type))

    def apply_checks(self, df: DataFrame, checks: list[DQRule]) -> DataFrame:
        """Applies data quality checks to a given dataframe.

        :param df: dataframe to check
        :param checks: list of checks to apply to the dataframe. Each check is an instance of DQRule class.
        :return: dataframe with errors and warning reporting columns
        """
        if not checks:
            return self._append_empty_checks(df)

        warning_checks = self._get_check_columns(checks, Criticality.WARN.value)
        error_checks = self._get_check_columns(checks, Criticality.ERROR.value)
        ndf = self._create_results_map(df, error_checks, Columns.ERRORS.value)
        ndf = self._create_results_map(ndf, warning_checks, Columns.WARNINGS.value)

        return ndf

    def apply_checks_and_split(self, df: DataFrame, checks: list[DQRule]) -> tuple[DataFrame, DataFrame]:
        """Applies data quality checks to a given dataframe and split it into two ("good" and "bad"),
        according to the data quality checks.

        :param df: dataframe to check
        :param checks: list of checks to apply to the dataframe. Each check is an instance of DQRule class.
        :return: two dataframes - "good" which includes warning rows but no reporting columns, and "data" having
        error and warning rows and corresponding reporting columns
        """
        if not checks:
            return df, self._append_empty_checks(df).limit(0)

        checked_df = self.apply_checks(df, checks)

        good_df = self.get_valid(checked_df)
        bad_df = self.get_invalid(checked_df)

        return good_df, bad_df

    @staticmethod
    def get_invalid(df: DataFrame) -> DataFrame:
        """
        Get records that violate data quality checks (records with warnings and errors).
        @param df: input DataFrame.
        @return: dataframe with error and warning rows and corresponding reporting columns.
        """
        return df.where(F.col(Columns.ERRORS.value).isNotNull() | F.col(Columns.WARNINGS.value).isNotNull())

    @staticmethod
    def get_valid(df: DataFrame) -> DataFrame:
        """
        Get records that don't violate data quality checks (records with warnings but no errors).
        @param df: input DataFrame.
        @return: dataframe with warning rows but no reporting columns.
        """
        return df.where(F.col(Columns.ERRORS.value).isNull()).drop(Columns.ERRORS.value, Columns.WARNINGS.value)

    @staticmethod
    def validate_checks(checks: list[dict], glbs: dict[str, Any] | None = None) -> ChecksValidationStatus:
        """
        Validate the input dict to ensure they conform to expected structure and types.

        Each check can be a dictionary. The function validates
        the presence of required keys, the existence and callability of functions, and the types
        of arguments passed to these functions.

        :param checks: List of checks to apply to the dataframe. Each check should be a dictionary.
        :param glbs: Optional dictionary of global functions that can be used in checks.

        :return ValidationStatus: The validation status.
        """
        status = ChecksValidationStatus()

        for check in checks:
            logger.debug(f"Processing check definition: {check}")
            if isinstance(check, dict):
                status.add_errors(DQEngine._validate_checks_dict(check, glbs))
            else:
                status.add_error(f"Unsupported check type: {type(check)}")

        return status

    @staticmethod
    def _validate_checks_dict(check: dict, glbs: dict[str, Any] | None) -> list[str]:
        """
        Validates the structure and content of a given check dictionary.

        Args:
            check (dict): The dictionary to validate.
            glbs (dict[str, Any] | None): A dictionary of global variables, or None.

        Returns:
            list[str]: The updated list of error messages.
        """
        errors: list[str] = []

        if "criticality" in check and check["criticality"] not in [c.value for c in Criticality]:
            errors.append(f"Invalid value for 'criticality' field: {check}")

        if "check" not in check:
            errors.append(f"'check' field is missing: {check}")
        elif not isinstance(check["check"], dict):
            errors.append(f"'check' field should be a dictionary: {check}")
        else:
            errors.extend(DQEngine._validate_check_block(check, glbs))

        return errors

    @staticmethod
    def _validate_check_block(check: dict, glbs: dict[str, Any] | None) -> list[str]:
        """
        Validates a check block within a configuration.

        Args:
            check (dict): The entire check configuration.
            glbs (dict[str, Any] | None): A dictionary of global functions or None.

        Returns:
            list[str]: The updated list of error messages.
        """
        check_block = check["check"]

        if "function" not in check_block:
            return [f"'function' field is missing in the 'check' block: {check}"]

        func_name = check_block["function"]
        func = DQEngine.resolve_function(func_name, glbs, fail_on_missing=False)
        if not callable(func):
            return [f"function '{func_name}' is not defined: {check}"]

        arguments = check_block.get("arguments", {})
        return DQEngine._validate_check_function_arguments(arguments, func, check)

    @staticmethod
    def _validate_check_function_arguments(arguments: dict, func: Callable, check: dict) -> list[str]:
        """
        Validates the provided arguments for a given function and updates the errors list if any validation fails.

        Args:
            arguments (dict): The arguments to validate.
            func (Callable): The function for which the arguments are being validated.
            check (dict): A dictionary containing the validation checks.

        Returns:
            list[str]: The updated list of error messages.
        """
        if not isinstance(arguments, dict):
            return [f"'arguments' should be a dictionary in the 'check' block: {check}"]

        if "col_names" in arguments:
            if not isinstance(arguments["col_names"], list):
                return [f"'col_names' should be a list in the 'arguments' block: {check}"]

            if len(arguments["col_names"]) == 0:
                return [f"'col_names' should not be empty in the 'arguments' block: {check}"]

            arguments = {
                'col_name' if k == 'col_names' else k: arguments['col_names'][0] if k == 'col_names' else v
                for k, v in arguments.items()
            }
            return DQEngine._validate_func_args(arguments, func, check)

        return DQEngine._validate_func_args(arguments, func, check)

    @staticmethod
    def _validate_func_args(arguments: dict, func: Callable, check: dict) -> list[str]:
        """
        Validates the arguments passed to a function against its signature.
        Args:
            arguments (dict): A dictionary of argument names and their values to be validated.
            func (Callable): The function whose arguments are being validated.
            check (dict): A dictionary containing additional context or information for error messages.
        Returns:
            list[str]: The updated list of error messages after validation.
        """

        @ft.lru_cache(None)
        def cached_signature(check_func):
            return inspect.signature(check_func)

        errors: list[str] = []
        sig = cached_signature(func)
        if not arguments and sig.parameters:
            errors.append(
                f"No arguments provided for function '{func.__name__}' in the 'arguments' block: {check}. "
                f"Expected arguments are: {list(sig.parameters.keys())}"
            )
        for arg, value in arguments.items():
            if arg not in sig.parameters:
                expected_args = list(sig.parameters.keys())
                errors.append(
                    f"Unexpected argument '{arg}' for function '{func.__name__}' in the 'arguments' block: {check}. "
                    f"Expected arguments are: {expected_args}"
                )
            else:
                expected_type = sig.parameters[arg].annotation
                if expected_type is not inspect.Parameter.empty and not isinstance(value, expected_type):
                    errors.append(
                        f"Argument '{arg}' should be of type '{expected_type.__name__}' for function '{func.__name__}' "
                        f"in the 'arguments' block: {check}"
                    )
        return errors

    @staticmethod
    def build_checks_by_metadata(checks: list[dict], glbs: dict[str, Any] | None = None) -> list[DQRule]:
        """Build checks based on check specification, i.e. function name plus arguments.

        :param checks: list of dictionaries describing checks. Each check is a dictionary consisting of following fields:
        * `check` - Column expression to evaluate. This expression should return string value if it's evaluated to true -
        it will be used as an error/warning message, or `null` if it's evaluated to `false`
        * `name` - name that will be given to a resulting column. Autogenerated if not provided
        * `criticality` (optional) - possible values are `error` (data going only into "bad" dataframe),
        and `warn` (data is going into both dataframes)
        :param glbs: dictionary with functions mapping (eg. ``globals()`` of the calling module).
        If not specified, then only built-in functions are used for the checks.
        :return: list of data quality check rules
        """
        status = DQEngine.validate_checks(checks, glbs)
        if status.has_errors:
            raise ValueError(str(status))

        dq_rule_checks = []
        for check_def in checks:
            logger.debug(f"Processing check definition: {check_def}")
            check = check_def.get("check", {})
            func_name = check.get("function", None)
            func = DQEngine.resolve_function(func_name, glbs, fail_on_missing=True)
            assert func  # should already be validated
            func_args = check.get("arguments", {})
            criticality = check_def.get("criticality", "error")

            if "col_names" in func_args:
                logger.debug(f"Adding DQRuleColSet with columns: {func_args['col_names']}")
                dq_rule_checks += DQRuleColSet(
                    columns=func_args["col_names"],
                    check_func=func,
                    criticality=criticality,
                    # provide arguments without "col_names"
                    check_func_kwargs={k: func_args[k] for k in func_args.keys() - {"col_names"}},
                ).get_rules()
            else:
                name = check_def.get("name", None)
                check_func = func(**func_args)
                dq_rule_checks.append(DQRule(check=check_func, name=name, criticality=criticality))

        logger.debug("Exiting build_checks_by_metadata function with dq_rule_checks")
        return dq_rule_checks

    @staticmethod
    def resolve_function(func_name: str, glbs: dict[str, Any] | None = None, fail_on_missing=True) -> Callable | None:
        logger.debug(f"Resolving function: {func_name}")
        if glbs:
            func = glbs.get(func_name)
        elif fail_on_missing:
            func = getattr(col_functions, func_name)
        else:
            func = getattr(col_functions, func_name, None)
        logger.debug(f"Function {func_name} resolved successfully")
        return func

    def apply_checks_by_metadata_and_split(
        self, df: DataFrame, checks: list[dict], glbs: dict[str, Any] | None = None
    ) -> tuple[DataFrame, DataFrame]:
        """Wrapper around `apply_checks_and_split` for use in the metadata-driven pipelines. The main difference
        is how the checks are specified - instead of using functions directly, they are described as function name plus
        arguments.

        :param df: dataframe to check
        :param checks: list of dictionaries describing checks. Each check is a dictionary consisting of following fields:
        * `check` - Column expression to evaluate. This expression should return string value if it's evaluated to true -
        it will be used as an error/warning message, or `null` if it's evaluated to `false`
        * `name` - name that will be given to a resulting column. Autogenerated if not provided
        * `criticality` (optional) - possible values are `error` (data going only into "bad" dataframe),
        and `warn` (data is going into both dataframes)
        :param glbs: dictionary with functions mapping (eg. ``globals()`` of the calling module).
        If not specified, then only built-in functions are used for the checks.
        :return: two dataframes - "good" which includes warning rows but no reporting columns, and "bad" having
        error and warning rows and corresponding reporting columns
        """
        dq_rule_checks = self.build_checks_by_metadata(checks, glbs)

        good_df, bad_df = self.apply_checks_and_split(df, dq_rule_checks)

        return good_df, bad_df

    def apply_checks_by_metadata(
        self, df: DataFrame, checks: list[dict], glbs: dict[str, Any] | None = None
    ) -> DataFrame:
        """Wrapper around `apply_checks` for use in the metadata-driven pipelines. The main difference
        is how the checks are specified - instead of using functions directly, they are described as function name plus
        arguments.

        :param df: dataframe to check
        :param checks: list of dictionaries describing checks. Each check is a dictionary consisting of following fields:
        * `check` - Column expression to evaluate. This expression should return string value if it's evaluated to true -
        it will be used as an error/warning message, or `null` if it's evaluated to `false`
        * `name` - name that will be given to a resulting column. Autogenerated if not provided
        * `criticality` (optional) - possible values are `error` (data going only into "bad" dataframe),
        and `warn` (data is going into both dataframes)
        :param glbs: dictionary with functions mapping (eg. ``globals()`` of calling module).
        If not specified, then only built-in functions are used for the checks.
        :return: dataframe with errors and warning reporting columns
        """
        dq_rule_checks = self.build_checks_by_metadata(checks, glbs)

        return self.apply_checks(df, dq_rule_checks)

    @staticmethod
    def build_checks(*rules_col_set: DQRuleColSet) -> list[DQRule]:
        """
        Build rules from dq rules and rule sets.

        :param rules_col_set: list of dq rules which define multiple columns for the same check function
        :return: list of dq rules
        """
        rules_nested = [rule_set.get_rules() for rule_set in rules_col_set]
        flat_rules = list(itertools.chain(*rules_nested))

        return list(filter(None, flat_rules))

    @staticmethod
    def load_checks_from_local_file(filename: str) -> list[dict]:
        """
        Load checks (dq rules) from a file (json or yml) in the local file system.
        This does not require installation of DQX in the workspace.
        The returning checks can be used as input for `apply_checks_by_metadata` function.

        :param filename: file name / path containing the checks.
        :return: list of dq rules
        """
        if not filename:
            raise ValueError("filename must be provided")

        try:
            checks = Installation.load_local(list[dict[str, str]], Path(filename))
            return DQEngine._deserialize_dicts(checks)
        except FileNotFoundError:
            msg = f"Checks file {filename} missing"
            raise FileNotFoundError(msg) from None

    def load_checks_from_workspace_file(self, workspace_path: str) -> list[dict]:
        """Load checks (dq rules) from a file (json or yml) in the workspace.
        This does not require installation of DQX in the workspace.
        The returning checks can be used as input for `apply_checks_by_metadata` function.

        :param workspace_path: path to the file in the workspace.
        :return: list of dq rules.
        """
        workspace_dir = os.path.dirname(workspace_path)
        filename = os.path.basename(workspace_path)
        installation = Installation(self.ws, "dqx", install_folder=workspace_dir)

        logger.info(f"Loading quality rules (checks) from {workspace_path} in the workspace.")
        return self._load_checks_from_file(installation, filename)

    def load_checks_from_installation(
        self, run_config_name: str | None = "default", product_name: str = "dqx", assume_user: bool = False
    ) -> list[dict]:
        """
        Load checks (dq rules) from a file (json or yml) defined in the installation config.
        The returning checks can be used as input for `apply_checks_by_metadata` function.

        :param run_config_name: name of the run (config) to use
        :param product_name: name of the product/installation directory
        :param assume_user: if True, assume user installation
        :return: list of dq rules
        """
        if assume_user:
            installation = Installation.assume_user_home(self.ws, product_name)
        else:
            installation = Installation.assume_global(self.ws, product_name)

        # verify the installation
        installation.current(self.ws, product_name, assume_user=assume_user)

        config = installation.load(WorkspaceConfig)
        run_config = config.get_run_config(run_config_name)
        filename = run_config.checks_file  # use check file from the config

        logger.info(f"Loading quality rules (checks) from {installation.install_folder()}/{filename} in the workspace.")
        return self._load_checks_from_file(installation, filename)

    def _load_checks_from_file(self, installation: Installation, filename: str) -> list[dict]:
        try:
            checks = installation.load(list[dict[str, str]], filename=filename)
            return self._deserialize_dicts(checks)
        except NotFound:
            msg = f"Checks file {filename} missing"
            raise NotFound(msg) from None

    @classmethod
    def _deserialize_dicts(cls, checks: list[dict[str, str]]) -> list[dict]:
        """
        deserialize string fields instances containing dictionaries
        @param checks: list of checks
        @return:
        """
        for item in checks:
            for key, value in item.items():
                if value.startswith("{") and value.endswith("}"):
                    item[key] = json.loads(value.replace("'", '"'))
        return checks
