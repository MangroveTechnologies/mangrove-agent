"""The user-chosen agent name, persisted with the installation's local state."""
from pydantic import BaseModel, field_validator
from pydantic import ValidationError as ModelValidationError

from src.shared.db.sqlite import get_connection
from src.shared.errors import ValidationError

_NAME_ERROR = "Choose an agent name of 1 to 64 printable characters."


class AgentNameRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 64 or not value.isprintable():
            raise ValueError(_NAME_ERROR)
        return value


def get_name() -> str | None:
    row = get_connection().execute("SELECT name FROM agent_identity WHERE id = 1").fetchone()
    return row["name"] if row else None


def set_name(name: str) -> dict:
    try:
        name = AgentNameRequest(name=name).name
    except ModelValidationError:
        raise ValidationError(_NAME_ERROR) from None
    conn = get_connection()
    conn.execute(
        "INSERT INTO agent_identity (id, name) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        (name,),
    )
    conn.commit()
    return {"agent_name": name}
