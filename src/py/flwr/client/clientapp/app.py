# Copyright 2024 Flower Labs GmbH. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Flower ClientApp process."""

import argparse
from logging import DEBUG, ERROR, INFO
from typing import Optional, Tuple

import grpc

from flwr.client.client_app import ClientApp, LoadClientAppError
from flwr.common import Context, Message
from flwr.common.constant import ErrorCode
from flwr.common.grpc import create_channel
from flwr.common.logger import log
from flwr.common.message import Error
from flwr.common.serde import (
    context_from_proto,
    context_to_proto,
    message_from_proto,
    message_to_proto,
    run_from_proto,
)
from flwr.common.typing import Run

# pylint: disable=E0611
from flwr.proto.clientappio_pb2 import (
    GetTokenRequest,
    GetTokenResponse,
    PullClientAppInputsRequest,
    PullClientAppInputsResponse,
    PushClientAppOutputsRequest,
    PushClientAppOutputsResponse,
)
from flwr.proto.clientappio_pb2_grpc import ClientAppIoStub

from .utils import get_load_client_app_fn


def flwr_clientapp() -> None:
    """Run process-isolated Flower ClientApp."""
    log(INFO, "Starting Flower ClientApp")

    parser = argparse.ArgumentParser(
        description="Run a Flower ClientApp",
    )
    parser.add_argument(
        "--supernode",
        type=str,
        help="Address of SuperNode ClientAppIo gRPC servicer",
    )
    parser.add_argument(
        "--token",
        type=int,
        required=False,
        help="Unique token generated by SuperNode for each ClientApp execution",
    )
    args = parser.parse_args()
    log(
        DEBUG,
        "Staring isolated `ClientApp` connected to SuperNode ClientAppIo at %s "
        "with the token %s",
        args.supernode,
        args.token,
    )
    run_clientapp(supernode=args.supernode, token=args.token)


def on_channel_state_change(channel_connectivity: str) -> None:
    """Log channel connectivity."""
    log(DEBUG, channel_connectivity)


def run_clientapp(  # pylint: disable=R0914
    supernode: str,
    token: Optional[int] = None,
) -> None:
    """Run Flower ClientApp process.

    Parameters
    ----------
    supernode : str
        Address of SuperNode
    token : Optional[int] (default: None)
        Unique SuperNode token for ClientApp-SuperNode authentication
    """
    channel = create_channel(
        server_address=supernode,
        insecure=True,
    )
    channel.subscribe(on_channel_state_change)

    try:
        stub = ClientAppIoStub(channel)

        # If token is not set, loop until token is received from SuperNode
        while token is None:
            token = get_token(stub)

        # Pull Message, Context, and Run from SuperNode
        message, context, run = pull_message(stub=stub, token=token)

        load_client_app_fn = get_load_client_app_fn(
            default_app_ref="",
            app_path=None,
            multi_app=True,
            flwr_dir=None,
        )

        try:
            # Load ClientApp
            client_app: ClientApp = load_client_app_fn(run.fab_id, run.fab_version)

            # Execute ClientApp
            reply_message = client_app(message=message, context=context)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            # Don't update/change NodeState

            e_code = ErrorCode.CLIENT_APP_RAISED_EXCEPTION
            # Ex fmt: "<class 'ZeroDivisionError'>:<'division by zero'>"
            reason = str(type(ex)) + ":<'" + str(ex) + "'>"
            exc_entity = "ClientApp"
            if isinstance(ex, LoadClientAppError):
                reason = "An exception was raised when attempting to load `ClientApp`"
                e_code = ErrorCode.LOAD_CLIENT_APP_EXCEPTION

            log(ERROR, "%s raised an exception", exc_entity, exc_info=ex)

            # Create error message
            reply_message = message.create_error_reply(
                error=Error(code=e_code, reason=reason)
            )

        # Push Message and Context to SuperNode
        _ = push_message(stub=stub, token=token, message=reply_message, context=context)

    except KeyboardInterrupt:
        log(INFO, "Closing connection")
    except grpc.RpcError as e:
        log(ERROR, "GRPC error occurred: %s", str(e))
    finally:
        channel.close()


def get_token(stub: grpc.Channel) -> Optional[int]:
    """Get a token from SuperNode."""
    log(DEBUG, "Flower ClientApp process requests token")
    res: GetTokenResponse = stub.GetToken(GetTokenRequest())
    return res.token


def pull_message(stub: grpc.Channel, token: int) -> Tuple[Message, Context, Run]:
    """Pull message from SuperNode to ClientApp."""
    res: PullClientAppInputsResponse = stub.PullClientAppInputs(
        PullClientAppInputsRequest(token=token)
    )
    message = message_from_proto(res.message)
    context = context_from_proto(res.context)
    run = run_from_proto(res.run)
    return message, context, run


def push_message(
    stub: grpc.Channel, token: int, message: Message, context: Context
) -> PushClientAppOutputsResponse:
    """Push message to SuperNode from ClientApp."""
    proto_message = message_to_proto(message)
    proto_context = context_to_proto(context)
    res: PushClientAppOutputsResponse = stub.PushClientAppOutputs(
        PushClientAppOutputsRequest(
            token=token, message=proto_message, context=proto_context
        )
    )
    return res
