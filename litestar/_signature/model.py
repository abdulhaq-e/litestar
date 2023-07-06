# ruff: noqa: UP006
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Optional, Sequence, Set, TypedDict, Union, cast

from msgspec import NODEFAULT, Meta, Struct, ValidationError, convert, defstruct
from msgspec.structs import asdict
from pydantic import ValidationError as PydanticValidationError
from typing_extensions import Annotated

from litestar._signature.utils import create_type_overrides, validate_signature_dependencies
from litestar.enums import ScopeType
from litestar.exceptions import InternalServerException, ValidationException
from litestar.params import DependencyKwarg, KwargDefinition, ParameterKwarg
from litestar.serialization import dec_hook
from litestar.typing import FieldDefinition  # noqa: TCH
from litestar.utils import make_non_optional_union
from litestar.utils.dataclass import simple_asdict
from litestar.utils.typing import unwrap_union

if TYPE_CHECKING:
    from typing_extensions import NotRequired

    from litestar.connection import ASGIConnection
    from litestar.types import AnyCallable
    from litestar.utils.signature import ParsedSignature


__all__ = (
    "ErrorMessage",
    "SignatureModel",
)


class ErrorMessage(TypedDict):
    # key may not be set in some cases, like when a query param is set but
    # doesn't match the required length during `attrs` validation
    # in this case, we don't show a key at all as it will be empty
    key: NotRequired[str]
    message: str
    source: NotRequired[Literal["cookie", "body", "header", "query"]]


MSGSPEC_CONSTRAINT_FIELDS = (
    "gt",
    "ge",
    "lt",
    "le",
    "multiple_of",
    "pattern",
    "min_length",
    "max_length",
)

ERR_RE = re.compile(r"`\$\.(.+)`$")


class SignatureModel(Struct):
    """Model that represents a function signature that uses a msgspec specific type or types."""

    dependency_name_set: ClassVar[Set[str]]
    return_annotation: ClassVar[Any]
    fields: ClassVar[dict[str, FieldDefinition]]

    @classmethod
    def _create_exception(cls, connection: ASGIConnection, messages: list[ErrorMessage]) -> Exception:
        """Create an exception class - either a ValidationException or an InternalServerException, depending on whether
            the failure is in client provided values or injected dependencies.

        Args:
            connection: An ASGI connection instance.
            messages: A list of error messages.

        Returns:
            An Exception
        """
        method = connection.method if hasattr(connection, "method") else ScopeType.WEBSOCKET  # pyright: ignore
        if client_errors := [
            err_message
            for err_message in messages
            if ("key" in err_message and err_message["key"] not in cls.dependency_name_set) or "key" not in err_message
        ]:
            return ValidationException(detail=f"Validation failed for {method} {connection.url}", extra=client_errors)
        return InternalServerException()

    @classmethod
    def _build_error_message(cls, keys: Sequence[str], exc_msg: str, connection: ASGIConnection) -> ErrorMessage:
        """Build an error message.

        Args:
            keys: A list of keys.
            exc_msg: A message.
            connection: An ASGI connection instance.

        Returns:
            An ErrorMessage
        """

        message: ErrorMessage = {"message": exc_msg.split(" - ")[0]}

        if not keys:
            return message

        message["key"] = key = ".".join(keys)

        if key in connection.query_params:
            message["source"] = cast("Literal['cookie', 'body', 'header', 'query']", "query")

        elif key in cls.fields and isinstance(cls.fields[key].kwarg_definition, ParameterKwarg):
            if cast(ParameterKwarg, cls.fields[key].kwarg_definition).cookie:
                source = "cookie"
            elif cast(ParameterKwarg, cls.fields[key].kwarg_definition).header:
                source = "header"
            else:
                source = "query"
            message["source"] = cast("Literal['cookie', 'body', 'header', 'query']", source)

        return message

    @classmethod
    def parse_values_from_connection_kwargs(cls, connection: ASGIConnection, **kwargs: Any) -> dict[str, Any]:
        """Extract values from the connection instance and return a dict of parsed values.

        Args:
            connection: The ASGI connection instance.
            **kwargs: A dictionary of kwargs.

        Raises:
            ValidationException: If validation failed.
            InternalServerException: If another exception has been raised.

        Returns:
            A dictionary of parsed values
        """
        messages: list[ErrorMessage] = []
        try:
            return convert(kwargs, cls, strict=False, dec_hook=dec_hook).to_dict()
        except PydanticValidationError as e:
            for exc in e.errors():
                keys = [str(loc) for loc in exc["loc"]]
                message = cls._build_error_message(keys=keys, exc_msg=exc["msg"], connection=connection)
                messages.append(message)
            raise cls._create_exception(messages=messages, connection=connection) from e
        except ValidationError as e:
            match = ERR_RE.search(str(e))
            keys = [str(match.group(1)) if match else "n/a"]
            message = cls._build_error_message(keys=keys, exc_msg=str(e), connection=connection)
            messages.append(message)
            raise cls._create_exception(messages=messages, connection=connection) from e

    def to_dict(self) -> dict[str, Any]:
        """Normalize access to the signature model's dictionary method, because different backends use different methods
        for this.

        Returns: A dictionary of string keyed values.
        """
        return asdict(self)

    @classmethod
    def create(
        cls,
        dependency_name_set: set[str],
        fn: AnyCallable,
        parsed_signature: ParsedSignature,
        has_data_dto: bool = False,
    ) -> type[SignatureModel]:
        fn_name = (
            fn_name if (fn_name := getattr(fn, "__name__", "anonymous")) and fn_name != "<lambda>" else "anonymous"
        )

        dependency_names = validate_signature_dependencies(
            dependency_name_set=dependency_name_set, fn_name=fn_name, parsed_signature=parsed_signature
        )
        type_overrides = create_type_overrides(parsed_signature, has_data_dto)

        struct_fields: list[tuple[str, Any, Any]] = []

        for field_definition in parsed_signature.parameters.values():
            annotation = type_overrides.get(field_definition.name, field_definition.annotation)

            if isinstance(field_definition.kwarg_definition, KwargDefinition):
                meta_kwargs: dict[str, Any] = {"extra": {}}

                kwarg_definition = simple_asdict(field_definition.kwarg_definition, exclude_empty=True)
                if min_items := kwarg_definition.pop("min_items", None):
                    meta_kwargs["min_length"] = min_items
                if max_items := kwarg_definition.pop("max_items", None):
                    meta_kwargs["max_length"] = max_items

                for k, v in kwarg_definition.items():
                    if hasattr(Meta, k) and v is not None:
                        meta_kwargs[k] = v
                    else:
                        meta_kwargs["extra"][k] = v

                meta = Meta(**meta_kwargs)
                if field_definition.is_optional:
                    annotation = Optional[Annotated[make_non_optional_union(annotation), meta]]
                elif field_definition.is_union and meta_kwargs.keys() & MSGSPEC_CONSTRAINT_FIELDS:
                    # unwrap inner types of a union and apply constraints to each individual type
                    # see https://github.com/jcrist/msgspec/issues/447
                    annotation = Union[
                        tuple(Annotated[inner_type, meta] for inner_type in unwrap_union(annotation))  # pyright: ignore
                    ]
                else:
                    annotation = Annotated[annotation, meta]

            elif (
                isinstance(field_definition.kwarg_definition, DependencyKwarg)
                and field_definition.kwarg_definition.skip_validation
            ):
                annotation = Any

            default = field_definition.default if field_definition.has_default else NODEFAULT
            struct_fields.append((field_definition.name, annotation, default))

        return defstruct(  # type:ignore[return-value]
            f"{fn_name}_signature_model",
            struct_fields,
            bases=(cls,),
            module=getattr(fn, "__module__", None),
            namespace={
                "return_annotation": parsed_signature.return_type.annotation,
                "dependency_name_set": dependency_names,
                "fields": parsed_signature.parameters,
            },
            kw_only=True,
        )
