"""SDK transport for the backend-owned on-chain operations."""

from mangrove_ai.exceptions import APIError
from starlette.concurrency import run_in_threadpool

from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import AgentError, SdkError, upstream_access_error


class OnChainUpstreamError(SdkError):
    def __init__(self, error: APIError):
        super().__init__(
            "The upstream on-chain request failed.", correlation_id=error.correlation_id
        )
        self.http_status = error.status_code
        self.upstream_code = error.code
        self.retry_after = error.retry_after

    def to_dict(self):
        return {
            **super().to_dict(),
            "upstream_status": self.http_status,
            "upstream_code": self.upstream_code,
            "retry_after": self.retry_after,
        }


async def read(operation, *args, **kwargs):
    def call():
        result = getattr(mangrove_ai_client().on_chain, operation)(*args, **kwargs)
        return result.model_dump() if hasattr(result, "model_dump") else result

    try:
        return await run_in_threadpool(call)
    except AgentError:
        raise
    except APIError as exc:
        denial = upstream_access_error(exc)
        if denial is not None:
            raise denial from None
        raise OnChainUpstreamError(exc) from None
    except Exception:
        raise SdkError("The upstream on-chain request could not be completed.") from None
