import enum
import json
import logging
import typing as t
import uuid
from collections import Counter
from functools import partial

from pydantic import BaseModel, Field, ValidationError, create_model, validator

from hrflow_connectors.core.templates import WORKFLOW_TEMPLATE
from hrflow_connectors.core.warehouse import Warehouse

logger = logging.getLogger(__name__)


class ConnectorActionAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: t.Dict) -> t.Tuple[str, t.Dict]:
        tags = [
            "[{}={}]".format(tag["name"], tag["value"])
            for tag in self.extra["log_tags"]
        ]
        return (
            "{}: {}".format(
                "".join(tags),
                msg,
            ),
            kwargs,
        )


class ActionRunEvents(enum.Enum):
    read_success = "read_success"
    read_failure = "read_failure"
    format_failure = "format_failure"
    logics_discard = "logics_discard"
    logics_failure = "logics_failure"
    write_failure = "write_failure"

    @classmethod
    def empty_counter(cls) -> t.Counter["ActionRunEvents"]:
        return Counter({event: 0 for event in cls})


class FatalError(enum.Enum):
    bad_action_parameters = "bad_action_parameters"
    bad_origin_parameters = "bad_origin_parameters"
    bad_target_parameters = "bad_target_parameters"
    format_failure = "format_failure"
    logics_failure = "logics_failure"
    read_failure = "read_failure"
    write_failure = "write_failure"
    none = ""


class ActionStatus(enum.Enum):
    success = "success"
    success_with_failures = "success_with_failures"
    fatal = "fatal"


class ActionRunResult(BaseModel):
    status: ActionStatus
    fatal_reason: FatalError = FatalError.none
    run_stats: t.Counter[ActionRunEvents] = Field(
        default_factory=ActionRunEvents.empty_counter
    )

    @classmethod
    def from_run_stats(cls, run_stats: t.Counter[ActionRunEvents]):
        read_success = run_stats[ActionRunEvents.read_success]
        read_failures = run_stats[ActionRunEvents.read_failure]
        if read_success == 0 and read_failures == 0:
            return cls(status=ActionStatus.success, run_stats=run_stats)
        elif read_success == 0 and read_failures > 0:
            return cls(
                status=ActionStatus.fatal,
                fatal_reason=FatalError.read_failure,
                run_stats=run_stats,
            )

        format_failures = run_stats[ActionRunEvents.format_failure]
        if format_failures == read_success:
            return cls(
                status=ActionStatus.fatal,
                fatal_reason=FatalError.format_failure,
                run_stats=run_stats,
            )

        logics_failures = run_stats[ActionRunEvents.logics_failure]
        if logics_failures == read_success - format_failures:
            return cls(
                status=ActionStatus.fatal,
                fatal_reason=FatalError.logics_failure,
                run_stats=run_stats,
            )

        logics_discard = run_stats[ActionRunEvents.logics_discard]
        write_failure = run_stats[ActionRunEvents.write_failure]
        if (
            write_failure
            == read_success - format_failures - logics_discard - logics_failures
        ) and write_failure > 0:
            return cls(
                status=ActionStatus.fatal,
                fatal_reason=FatalError.write_failure,
                run_stats=run_stats,
            )

        success_with_failures = any(
            run_stats[event] > 0
            for event in [
                ActionRunEvents.read_failure,
                ActionRunEvents.format_failure,
                ActionRunEvents.logics_failure,
                ActionRunEvents.write_failure,
            ]
        )
        if success_with_failures:
            return cls(status=ActionStatus.success_with_failures, run_stats=run_stats)
        return cls(status=ActionStatus.success, run_stats=run_stats)


LogicFunctionType = t.Callable[[t.Dict], t.Union[t.Dict, None]]
LogicsTemplate = """
import typing as t

def logic_1(item: t.Dict) -> t.Union[t.Dict, None]:
    return None

def logic_2(item: t.Dict) -> t.Uniont[t.Dict, None]:
    return None

logics = [logic_1, logic_2]
"""
LogicsDescription = "List of logic functions"
FormatFunctionType = t.Callable[[t.Dict], t.Dict]
FormatTemplate = """
import typing as t

def format(item: t.Dict) -> t.Dict:
    return item
"""
FormatDescription = "Formatting function"


class BaseActionParameters(BaseModel):
    logics: t.List[LogicFunctionType] = Field(
        default_factory=list, description=LogicsDescription
    )
    format: FormatFunctionType = Field(lambda x: x, description=FormatDescription)

    class Config:
        @staticmethod
        def schema_extra(
            schema: t.Dict[str, t.Any], model: t.Type["BaseActionParameters"]
        ) -> None:
            # JSON has no equivalent for Callable type which is used for
            # logics and format. Thus we hardcode properties for both here
            schema["properties"]["logics"] = (
                {
                    "title": "logics",
                    "description": (
                        "List of logic functions. Each function should have"
                        " the following signature {}. The final list should be exposed "
                        "in a variable named 'logics'.".format(LogicFunctionType)
                    ),
                    "template": LogicsTemplate,
                    "type": "code_editor",
                },
            )
            schema["properties"]["format"] = (
                {
                    "title": "format",
                    "description": (
                        "Formatting function. You should expose a function"
                        " named 'format' with following signature {}".format(
                            FormatFunctionType
                        )
                    ),
                    "template": FormatTemplate,
                    "type": "code_editor",
                },
            )

    @classmethod
    def with_default_format(
        cls, model_name: str, format: FormatFunctionType
    ) -> t.Type["BaseActionParameters"]:
        return create_model(
            model_name,
            format=(
                FormatFunctionType,
                Field(format, description=FormatDescription),
            ),
            __base__=cls,
        )


class WorkflowType(str, enum.Enum):
    catch = "catch"
    pull = "pull"


class ConnectorAction(BaseModel):
    WORKFLOW_FORMAT_PLACEHOLDER = "# << format_placeholder >>"
    WORKFLOW_LOGICS_PLACEHOLDER = "# << logics_placeholder >>"

    name: str
    type: WorkflowType
    description: str
    parameters: t.Type[BaseModel]
    origin: Warehouse
    target: Warehouse

    @validator("origin", pre=False)
    def origin_is_readable(cls, origin):
        if origin.is_readable is False:
            raise ValueError("Origin warehouse is not readable")
        return origin

    @validator("target", pre=False)
    def target_is_writable(cls, target):
        if target.is_writable is False:
            raise ValueError("Target warehouse is not writable")
        return target

    def workflow_code(self, connector_name: str) -> str:
        return WORKFLOW_TEMPLATE.render(
            format_placeholder=self.WORKFLOW_FORMAT_PLACEHOLDER,
            logics_placeholder=self.WORKFLOW_LOGICS_PLACEHOLDER,
            connector_name=connector_name,
            action_name=self.name,
            type=self.type.value,
            origin_parameters=[
                parameter for parameter in self.origin.read.parameters.__fields__
            ],
            target_parameters=[
                parameter for parameter in self.target.write.parameters.__fields__
            ],
        )

    def run(
        self,
        connector_name: str,
        action_parameters: t.Dict,
        origin_parameters: t.Dict,
        target_parameters: t.Dict,
    ) -> ActionRunResult:
        action_id = uuid.uuid4()
        adapter = ConnectorActionAdapter(
            logger,
            dict(
                log_tags=[
                    dict(name="connector", value=connector_name),
                    dict(name="action_name", value=self.name),
                    dict(name="action_id", value=action_id),
                ]
            ),
        )
        adapter.info("Starting Action")

        try:
            parameters = self.parameters(**action_parameters)
        except ValidationError as e:
            adapter.warning(
                "Failed to parse action_parameters with errors={}".format(e.errors())
            )
            return ActionRunResult(
                status=ActionStatus.fatal, fatal_reason=FatalError.bad_action_parameters
            )

        try:
            origin_parameters = self.origin.read.parameters(**origin_parameters)
        except ValidationError as e:
            adapter.warning(
                "Failed to parse origin_parameters with errors={}".format(e.errors())
            )
            return ActionRunResult(
                status=ActionStatus.fatal, fatal_reason=FatalError.bad_origin_parameters
            )

        try:
            target_parameters = self.target.write.parameters(**target_parameters)
        except ValidationError as e:
            adapter.warning(
                "Failed to parse target_parameters with errors={}".format(e.errors())
            )
            return ActionRunResult(
                status=ActionStatus.fatal, fatal_reason=FatalError.bad_target_parameters
            )

        run_stats = ActionRunEvents.empty_counter()

        adapter.info(
            "Starting to read from warehouse={} with parameters={}".format(
                self.origin.name, origin_parameters
            )
        )
        origin_adapter = ConnectorActionAdapter(
            logger,
            dict(
                log_tags=adapter.extra["log_tags"]
                + [
                    dict(name="warehouse", value=self.origin.name),
                    dict(name="action", value="read"),
                ]
            ),
        )
        origin_items = []
        try:
            for item in self.origin.read(origin_adapter, origin_parameters):
                origin_items.append(item)
                run_stats[ActionRunEvents.read_success] += 1
        except Exception as e:
            run_stats[ActionRunEvents.read_failure] += 1
            adapter.error(
                "Failed to read from warehouse={} with parameters={} error={}".format(
                    self.origin.name, origin_parameters, repr(e)
                )
            )
        adapter.info(
            "Finished reading from warehouse={} n_items={} read_failure={}".format(
                self.origin.name,
                len(origin_items),
                run_stats[ActionRunEvents.read_failure] > 0,
            )
        )

        using_default_format = not bool(action_parameters.get("format"))
        adapter.info(
            "Starting to format origin items using {} function".format(
                "default" if using_default_format else "user defined"
            )
        )
        formatted_items = []
        for item in origin_items:
            try:
                formatted_items.append(parameters.format(item))
            except Exception as e:
                run_stats[ActionRunEvents.format_failure] += 1
                adapter.error(
                    "Failed to format origin item using {} function error={}".format(
                        "default" if using_default_format else "user defined", repr(e)
                    )
                )
        adapter.info(
            "Finished formatting origin items success={} failures={}".format(
                len(formatted_items), run_stats[ActionRunEvents.format_failure]
            )
        )

        if len(parameters.logics) > 0:
            adapter.info(
                "Starting to apply logic functions: "
                "n_items={} before applying logics".format(len(formatted_items))
            )
            items_to_write = []
            for item in formatted_items:
                for i, logic in enumerate(parameters.logics):
                    try:
                        item = logic(item)
                    except Exception as e:
                        adapter.error(
                            "Failed to apply logic function number={} error={}".format(
                                i, repr(e)
                            )
                        )
                        run_stats[ActionRunEvents.logics_failure] += 1
                        break
                    if item is None:
                        run_stats[ActionRunEvents.logics_discard] += 1
                        break
                else:
                    items_to_write.append(item)
            adapter.info(
                "Finished applying logic functions: "
                "success={} discarded={} failures={}".format(
                    len(items_to_write),
                    run_stats[ActionRunEvents.logics_discard],
                    run_stats[ActionRunEvents.logics_failure],
                )
            )
        else:
            adapter.info("No logic functions supplied. Skipping")
            items_to_write = formatted_items

        adapter.info(
            "Starting to write to warehouse={} with parameters={} n_items={}".format(
                self.target.name, target_parameters, len(items_to_write)
            )
        )
        target_adapter = ConnectorActionAdapter(
            logger,
            dict(
                log_tags=adapter.extra["log_tags"]
                + [
                    dict(name="warehouse", value=self.target.name),
                    dict(name="action", value="write"),
                ]
            ),
        )
        try:
            failed_items = self.target.write(
                target_adapter, target_parameters, items_to_write
            )
            run_stats[ActionRunEvents.write_failure] += len(failed_items)
        except Exception as e:
            adapter.error(
                "Failed to write to warehouse={} with parameters={} error={}".format(
                    self.target.name, target_parameters, repr(e)
                )
            )
            run_stats[ActionRunEvents.write_failure] += len(items_to_write)
        adapter.info(
            "Finished writing to warehouse={} success={} failures={}".format(
                self.target.name,
                len(items_to_write) - run_stats[ActionRunEvents.write_failure],
                run_stats[ActionRunEvents.write_failure],
            )
        )
        adapter.info("Finished action")
        return ActionRunResult.from_run_stats(run_stats)


class ConnectorModel(BaseModel):
    name: str
    description: str
    url: str
    actions: t.List[ConnectorAction]


class Connector:
    def __init__(self, *args, **kwargs) -> None:
        self.model = ConnectorModel(*args, **kwargs)
        for action in self.model.actions:
            with_connector_name = partial(action.run, connector_name=self.model.name)
            setattr(self, action.name, with_connector_name)

    def manifest(self) -> t.Dict:
        model = self.model
        manifest = dict(name=model.name, actions=[])
        for action in model.actions:
            action_manifest = dict(
                name=action.name,
                action_parameters=action.parameters.schema(),
                origin=action.origin.name,
                origin_parameters=action.origin.read.parameters.schema(),
                origin_data_schema=action.origin.data_schema.schema(),
                target=action.target.name,
                target_parameters=action.target.write.parameters.schema(),
                target_data_schema=action.target.data_schema.schema(),
                workflow_type=action.type,
                workflow_code=action.workflow_code(connector_name=model.name),
                workflow_code_format_placeholder=action.WORKFLOW_FORMAT_PLACEHOLDER,
                workflow_code_logics_placeholder=action.WORKFLOW_LOGICS_PLACEHOLDER,
            )
            manifest["actions"].append(action_manifest)
        return manifest


def hrflow_connectors_manifest(
    connectors: t.List[Connector], directory_path: str = "."
) -> None:
    manifest = dict(
        name="HrFlow.ai Connectors",
        connectors=[connector.manifest() for connector in connectors],
    )
    with open("{}/manifest.json".format(directory_path), "w") as f:
        json.dump(manifest, f, indent=2)
