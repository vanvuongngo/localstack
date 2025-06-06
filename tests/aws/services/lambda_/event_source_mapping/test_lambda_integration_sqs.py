import json
import math
import time

import pytest
from botocore.exceptions import ClientError
from localstack_snapshot.snapshots.transformer import KeyValueBasedTransformer, SortingTransformer

from localstack.aws.api.lambda_ import InvalidParameterValueException, Runtime
from localstack.config import is_env_true
from localstack.testing.aws.lambda_utils import _await_event_source_mapping_enabled
from localstack.testing.aws.util import is_aws_cloud
from localstack.testing.pytest import markers
from localstack.utils.strings import short_uid
from localstack.utils.sync import retry
from localstack.utils.testutil import check_expected_lambda_log_events_length, get_lambda_log_events
from tests.aws.services.lambda_.functions import FUNCTIONS_PATH, lambda_integration
from tests.aws.services.lambda_.test_lambda import (
    TEST_LAMBDA_EVENT_SOURCE_MAPPING_SEND_MESSAGE,
    TEST_LAMBDA_PYTHON,
    TEST_LAMBDA_PYTHON_ECHO,
    TEST_LAMBDA_PYTHON_ECHO_VERSION_ENV,
)

LAMBDA_SQS_INTEGRATION_FILE = FUNCTIONS_PATH / "lambda_sqs_integration.py"
LAMBDA_SQS_BATCH_ITEM_FAILURE_FILE = FUNCTIONS_PATH / "lambda_sqs_batch_item_failure.py"
LAMBDA_SLEEP_FILE = FUNCTIONS_PATH / "lambda_sleep.py"
# AWS API reference:
# https://docs.aws.amazon.com/lambda/latest/dg/API_CreateEventSourceMapping.html#SSS-CreateEventSourceMapping-request-BatchSize
DEFAULT_SQS_BATCH_SIZE = 10
MAX_SQS_BATCH_SIZE_FIFO = 10


@pytest.fixture(autouse=True)
def _snapshot_transformers(snapshot):
    # manual transformers since we are passing SQS attributes through lambdas and back again
    snapshot.add_transformer(snapshot.transform.key_value("QueueUrl"))
    snapshot.add_transformer(snapshot.transform.key_value("ReceiptHandle"))
    snapshot.add_transformer(snapshot.transform.key_value("SenderId", reference_replacement=False))
    snapshot.add_transformer(snapshot.transform.key_value("SequenceNumber"))
    snapshot.add_transformer(snapshot.transform.resource_name())
    # body contains dynamic attributes so md5 hash changes
    snapshot.add_transformer(snapshot.transform.key_value("MD5OfBody"))
    # lower-case for when messages are rendered in lambdas
    snapshot.add_transformer(snapshot.transform.key_value("receiptHandle"))
    snapshot.add_transformer(snapshot.transform.key_value("md5OfBody"))
    # transform UNIX timestamps
    snapshot.add_transformer(
        snapshot.transform.key_value("SentTimestamp", reference_replacement=False)
    )
    snapshot.add_transformer(
        KeyValueBasedTransformer(
            lambda k, v: str(v) if k == "ApproximateFirstReceiveTimestamp" else None,
            "<approximate-first-receive-timestamp>",
            replace_reference=False,
        )
    )


@markers.aws.validated
def test_esm_with_not_existing_sqs_queue(
    aws_client, account_id, region_name, create_lambda_function, lambda_su_role, snapshot
):
    function_name = f"simple-lambda-{short_uid()}"
    create_lambda_function(
        func_name=function_name,
        handler_file=LAMBDA_SQS_INTEGRATION_FILE,
        runtime=Runtime.python3_12,
        role=lambda_su_role,
        timeout=5,
    )
    not_existing_queue_arn = (
        f"arn:aws:sqs:{region_name}:{account_id}:not-existing-queue-{short_uid()}"
    )
    with pytest.raises(ClientError) as e:
        aws_client.lambda_.create_event_source_mapping(
            EventSourceArn=not_existing_queue_arn,
            FunctionName=function_name,
            BatchSize=1,
        )
    snapshot.match("error", e.value.response)


@markers.aws.validated
def test_failing_lambda_retries_after_visibility_timeout(
    create_lambda_function,
    sqs_create_queue,
    sqs_get_queue_arn,
    lambda_su_role,
    snapshot,
    cleanups,
    aws_client,
):
    """This test verifies a basic SQS retry scenario. The lambda uses an SQS queue as event source, and we are
    testing whether the lambda automatically retries after the visibility timeout expires, and, after the retry,
    properly deletes the message from the queue."""

    # create queue used in the lambda to send events to (to verify lambda was invoked)
    destination_queue_name = f"destination-queue-{short_uid()}"
    destination_url = sqs_create_queue(QueueName=destination_queue_name)
    snapshot.match(
        "get_destination_queue_url", aws_client.sqs.get_queue_url(QueueName=destination_queue_name)
    )

    # timeout in seconds, used for both the lambda and the queue visibility timeout
    retry_timeout = 5

    # set up lambda function
    function_name = f"failing-lambda-{short_uid()}"
    create_lambda_function(
        func_name=function_name,
        handler_file=LAMBDA_SQS_INTEGRATION_FILE,
        runtime=Runtime.python3_12,
        role=lambda_su_role,
        timeout=retry_timeout,  # timeout needs to be <= than visibility timeout
    )

    # create event source queue
    event_source_url = sqs_create_queue(
        QueueName=f"source-queue-{short_uid()}",
        Attributes={
            # the visibility timeout is implicitly also the time between retries
            "VisibilityTimeout": str(retry_timeout),
        },
    )
    event_source_arn = sqs_get_queue_arn(event_source_url)

    # wire everything with the event source mapping
    response = aws_client.lambda_.create_event_source_mapping(
        EventSourceArn=event_source_arn,
        FunctionName=function_name,
        BatchSize=1,
    )
    mapping_uuid = response["UUID"]
    cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
    _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)
    response = aws_client.lambda_.get_event_source_mapping(UUID=mapping_uuid)
    snapshot.match("event_source_mapping", response)

    # trigger lambda with a message and pass the result destination url. the event format is expected by the
    # lambda_sqs_integration.py lambda.
    event = {"destination": destination_url, "fail_attempts": 1}
    aws_client.sqs.send_message(
        QueueUrl=event_source_url,
        MessageBody=json.dumps(event),
    )

    # now wait for the first invocation result which is expected to fail
    then = time.time()
    first_response = aws_client.sqs.receive_message(
        QueueUrl=destination_url, WaitTimeSeconds=15, MaxNumberOfMessages=1
    )
    snapshot.match("first_attempt", first_response)

    # and then after a few seconds (at least the visibility timeout), we expect the
    second_response = aws_client.sqs.receive_message(
        QueueUrl=destination_url, WaitTimeSeconds=15, MaxNumberOfMessages=1
    )
    snapshot.match("second_attempt", second_response)

    # check that it took at least the retry timeout between the first and second attempt
    assert time.time() >= then + retry_timeout

    # assert message is removed from the queue
    third_response = aws_client.sqs.receive_message(
        QueueUrl=destination_url, WaitTimeSeconds=retry_timeout + 1, MaxNumberOfMessages=1
    )
    assert "Messages" not in third_response or third_response["Messages"] == []


@markers.snapshot.skip_snapshot_verify(
    paths=[
        # AWS returns empty lists for these values, even though they are not implemented yet
        # https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_MessageAttributeValue.html
        "$..stringListValues",
        "$..binaryListValues",
    ]
)
@markers.aws.validated
def test_message_body_and_attributes_passed_correctly(
    create_lambda_function,
    sqs_create_queue,
    sqs_get_queue_arn,
    lambda_su_role,
    snapshot,
    cleanups,
    aws_client,
):
    # create queue used in the lambda to send events to (to verify lambda was invoked)
    destination_queue_name = f"destination-queue-{short_uid()}"
    destination_url = sqs_create_queue(QueueName=destination_queue_name)
    snapshot.match(
        "get_destination_queue_url", aws_client.sqs.get_queue_url(QueueName=destination_queue_name)
    )

    # timeout in seconds, used for both the lambda and the queue visibility timeout
    retry_timeout = 5
    retries = 2

    # set up lambda function
    function_name = f"lambda-{short_uid()}"
    create_lambda_function(
        func_name=function_name,
        handler_file=LAMBDA_SQS_INTEGRATION_FILE,
        runtime=Runtime.python3_12,
        role=lambda_su_role,
        timeout=retry_timeout,  # timeout needs to be <= than visibility timeout
    )

    # create dlq for event source queue
    event_dlq_url = sqs_create_queue(QueueName=f"event-dlq-{short_uid()}")
    event_dlq_arn = sqs_get_queue_arn(event_dlq_url)

    # create event source queue
    event_source_url = sqs_create_queue(
        QueueName=f"source-queue-{short_uid()}",
        Attributes={
            # the visibility timeout is implicitly also the time between retries
            "VisibilityTimeout": str(retry_timeout),
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": event_dlq_arn, "maxReceiveCount": retries}
            ),
        },
    )
    event_source_arn = sqs_get_queue_arn(event_source_url)

    # wire everything with the event source mapping
    mapping_uuid = aws_client.lambda_.create_event_source_mapping(
        EventSourceArn=event_source_arn,
        FunctionName=function_name,
        BatchSize=1,
    )["UUID"]
    cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
    _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

    # trigger lambda with a message and pass the result destination url. the event format is expected by the
    # lambda_sqs_integration.py lambda.
    event = {"destination": destination_url, "fail_attempts": 0}
    aws_client.sqs.send_message(
        QueueUrl=event_source_url,
        MessageBody=json.dumps(event),
        MessageAttributes={
            "Title": {"DataType": "String", "StringValue": "The Whistler"},
            "Author": {"DataType": "String", "StringValue": "John Grisham"},
            "WeeksOn": {"DataType": "Number", "StringValue": "6"},
        },
    )

    # now wait for the first invocation result which is expected to fail
    response = aws_client.sqs.receive_message(
        QueueUrl=destination_url,
        WaitTimeSeconds=15,
        MaxNumberOfMessages=1,
    )
    assert "Messages" in response
    snapshot.match("first_attempt", response)


@markers.aws.validated
def test_redrive_policy_with_failing_lambda(
    create_lambda_function,
    sqs_create_queue,
    sqs_get_queue_arn,
    lambda_su_role,
    snapshot,
    cleanups,
    aws_client,
):
    """This test verifies that SQS moves a message that is passed to a failing lambda to a DLQ according to the
    redrive policy, and the lambda is invoked the correct number of times. The test retries twice and the event
    source mapping should then automatically move the message to the DLQ, but not earlier (see
    https://github.com/localstack/localstack/issues/5283)"""

    # create queue used in the lambda to send events to (to verify lambda was invoked)
    destination_queue_name = f"destination-queue-{short_uid()}"
    destination_url = sqs_create_queue(QueueName=destination_queue_name)
    snapshot.match(
        "get_destination_queue_url", aws_client.sqs.get_queue_url(QueueName=destination_queue_name)
    )

    # timeout in seconds, used for both the lambda and the queue visibility timeout
    retry_timeout = 5
    retries = 2

    # set up lambda function
    function_name = f"failing-lambda-{short_uid()}"
    create_lambda_function(
        func_name=function_name,
        handler_file=LAMBDA_SQS_INTEGRATION_FILE,
        runtime=Runtime.python3_12,
        role=lambda_su_role,
        timeout=retry_timeout,  # timeout needs to be <= than visibility timeout
    )

    # create dlq for event source queue
    event_dlq_url = sqs_create_queue(QueueName=f"event-dlq-{short_uid()}")
    event_dlq_arn = sqs_get_queue_arn(event_dlq_url)

    # create event source queue
    event_source_url = sqs_create_queue(
        QueueName=f"source-queue-{short_uid()}",
        Attributes={
            # the visibility timeout is implicitly also the time between retries
            "VisibilityTimeout": str(retry_timeout),
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": event_dlq_arn, "maxReceiveCount": retries}
            ),
        },
    )
    event_source_arn = sqs_get_queue_arn(event_source_url)

    # wire everything with the event source mapping
    mapping_uuid = aws_client.lambda_.create_event_source_mapping(
        EventSourceArn=event_source_arn,
        FunctionName=function_name,
        BatchSize=1,
    )["UUID"]
    cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
    _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

    # trigger lambda with a message and pass the result destination url. the event format is expected by the
    # lambda_sqs_integration.py lambda.
    event = {"destination": destination_url, "fail_attempts": retries}
    aws_client.sqs.send_message(
        QueueUrl=event_source_url,
        MessageBody=json.dumps(event),
    )

    # now wait for the first invocation result which is expected to fail
    first_response = aws_client.sqs.receive_message(
        QueueUrl=destination_url, WaitTimeSeconds=15, MaxNumberOfMessages=1
    )
    snapshot.match("first_attempt", first_response)

    # check that the DLQ is empty
    second_response = aws_client.sqs.receive_message(QueueUrl=event_dlq_url, WaitTimeSeconds=1)
    assert "Messages" not in second_response or second_response["Messages"] == []

    # the second is also expected to fail, and then the message moves into the DLQ
    third_response = aws_client.sqs.receive_message(
        QueueUrl=destination_url, WaitTimeSeconds=15, MaxNumberOfMessages=1
    )
    snapshot.match("second_attempt", third_response)

    # now check that the event messages was placed in the DLQ
    dlq_response = aws_client.sqs.receive_message(QueueUrl=event_dlq_url, WaitTimeSeconds=15)
    snapshot.match("dlq_response", dlq_response)


@markers.aws.validated
def test_sqs_queue_as_lambda_dead_letter_queue(
    lambda_su_role,
    create_lambda_function,
    sqs_create_queue,
    sqs_get_queue_arn,
    snapshot,
    aws_client,
):
    snapshot.add_transformer(
        [
            # MessageAttributes contain the request id, messes the hash
            snapshot.transform.key_value(
                "MD5OfMessageAttributes",
                value_replacement="<md5-hash>",
                reference_replacement=False,
            ),
            snapshot.transform.jsonpath(
                "$..Messages..MessageAttributes.RequestID.StringValue", "request-id"
            ),
        ]
    )

    dlq_queue_url = sqs_create_queue()
    dlq_queue_arn = sqs_get_queue_arn(dlq_queue_url)

    function_name = f"lambda-fn-{short_uid()}"
    lambda_creation_response = create_lambda_function(
        func_name=function_name,
        handler_file=TEST_LAMBDA_PYTHON,
        runtime=Runtime.python3_12,
        role=lambda_su_role,
        DeadLetterConfig={"TargetArn": dlq_queue_arn},
    )
    snapshot.match(
        "lambda-response-dlq-config",
        lambda_creation_response["CreateFunctionResponse"]["DeadLetterConfig"],
    )

    # Set retries to zero to speed up the test
    aws_client.lambda_.put_function_event_invoke_config(
        FunctionName=function_name,
        MaximumRetryAttempts=0,
    )

    # invoke Lambda, triggering an error
    payload = {lambda_integration.MSG_BODY_RAISE_ERROR_FLAG: 1}
    aws_client.lambda_.invoke(
        FunctionName=function_name,
        Payload=json.dumps(payload),
        InvocationType="Event",
    )

    def receive_dlq():
        result = aws_client.sqs.receive_message(
            QueueUrl=dlq_queue_url, MessageAttributeNames=["All"], VisibilityTimeout=0
        )
        assert len(result["Messages"]) > 0
        return result

    # It can take ~3 min against AWS until the message is received
    sleep = 15 if is_aws_cloud() else 5
    messages = retry(receive_dlq, retries=15, sleep=sleep, sleep_before=5)

    snapshot.match("messages", messages)


@markers.aws.validated
def test_report_batch_item_failures(
    create_lambda_function,
    sqs_create_queue,
    sqs_get_queue_arn,
    lambda_su_role,
    snapshot,
    cleanups,
    aws_client,
):
    """This test verifies the SQS Lambda integration feature Reporting batch item failures
    redrive policy, and the lambda is invoked the correct number of times. The test retries twice and the event
    source mapping should then automatically move the message to the DLQ, but not earlier (see
    https://github.com/localstack/localstack/issues/5283)"""

    # create queue used in the lambda to send invocation results to (to verify lambda was invoked)
    destination_queue_name = f"destination-queue-{short_uid()}"
    destination_url = sqs_create_queue(QueueName=destination_queue_name)
    snapshot.match(
        "get_destination_queue_url", aws_client.sqs.get_queue_url(QueueName=destination_queue_name)
    )

    # If an SQS queue is not receiving a lot of traffic, Lambda can take up to 20s between invocations.
    # See AWS docs https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html.
    retry_timeout = 6
    visibility_timeout = 8
    retries = 2

    # AWS recommends a visibility timeout should be x6 a Lambda's retry timeout. To ensure a short test
    # runtime, we just want to ensure messages are re-queued a couple of seconda after any potential timeouts.
    # See https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html#events-sqs-queueconfig
    assert visibility_timeout > retry_timeout, (
        "A lambda needs to finish processing prior to re-queuing invisible messages"
    )

    # set up lambda function
    function_name = f"failing-lambda-{short_uid()}"
    create_lambda_function(
        func_name=function_name,
        handler_file=LAMBDA_SQS_BATCH_ITEM_FAILURE_FILE,
        runtime=Runtime.python3_12,
        role=lambda_su_role,
        timeout=retry_timeout,
        envvars={"DESTINATION_QUEUE_URL": destination_url},
    )

    # create dlq for event source queue
    event_dlq_url = sqs_create_queue(
        QueueName=f"event-dlq-{short_uid()}.fifo", Attributes={"FifoQueue": "true"}
    )
    event_dlq_arn = sqs_get_queue_arn(event_dlq_url)

    # create event source queue
    # we use a FIFO queue to be sure the lambda is invoked in a deterministic way
    event_source_url = sqs_create_queue(
        QueueName=f"source-queue-{short_uid()}.fifo",
        Attributes={
            "FifoQueue": "true",
            # the visibility timeout is implicitly also the time between retries
            "VisibilityTimeout": str(visibility_timeout),
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": event_dlq_arn, "maxReceiveCount": retries}
            ),
        },
    )
    event_source_arn = sqs_get_queue_arn(event_source_url)

    # put a batch in the queue. the event format is expected by the lambda_sqs_batch_item_failure.py lambda.
    # we add the batch before the event_source_mapping to be sure that the entire batch is sent to the first invocation.
    # message 1 succeeds immediately
    # message 2 and 3 succeeds after one retry
    # message 4 fails after 2 retries and lands in the DLQ
    response = aws_client.sqs.send_message_batch(
        QueueUrl=event_source_url,
        Entries=[
            {
                "Id": "message-1",
                "MessageBody": json.dumps({"message": 1, "fail_attempts": 0}),
                "MessageGroupId": "1",
                "MessageDeduplicationId": "dedup-1",
            },
            {
                "Id": "message-2",
                "MessageBody": json.dumps({"message": 2, "fail_attempts": 1}),
                "MessageGroupId": "1",
                "MessageDeduplicationId": "dedup-2",
            },
            {
                "Id": "message-3",
                "MessageBody": json.dumps({"message": 3, "fail_attempts": 1}),
                "MessageGroupId": "1",
                "MessageDeduplicationId": "dedup-3",
            },
            {
                "Id": "message-4",
                "MessageBody": json.dumps({"message": 4, "fail_attempts": retries}),
                "MessageGroupId": "1",
                "MessageDeduplicationId": "dedup-4",
            },
        ],
    )
    # sort so snapshotting works
    response["Successful"].sort(key=lambda r: r["Id"])
    snapshot.match("send_message_batch", response)

    # wait for all items to appear in the queue
    _await_queue_size(aws_client.sqs, event_source_url, qsize=4, retries=30)

    # wire everything with the event source mapping
    response = aws_client.lambda_.create_event_source_mapping(
        EventSourceArn=event_source_arn,
        FunctionName=function_name,
        BatchSize=10,
        MaximumBatchingWindowInSeconds=0,
        FunctionResponseTypes=["ReportBatchItemFailures"],
    )
    snapshot.match("create_event_source_mapping", response)
    mapping_uuid = response["UUID"]
    cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
    _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

    # now wait for the first invocation result which is expected to have processed message 1 we wait half the retry
    # interval to wait long enough for the message to appear, but short enough to check that the DLQ is empty after
    # the first attempt.
    first_invocation = aws_client.sqs.receive_message(
        QueueUrl=destination_url, WaitTimeSeconds=int(retry_timeout / 2), MaxNumberOfMessages=1
    )
    # hack to make snapshot work
    first_invocation["Messages"][0]["Body"] = json.loads(first_invocation["Messages"][0]["Body"])
    first_invocation["Messages"][0]["Body"]["event"]["Records"].sort(
        key=lambda record: json.loads(record["body"])["message"]
    )
    snapshot.match("first_invocation", first_invocation)

    # check that the DLQ is empty
    dlq_messages = aws_client.sqs.receive_message(QueueUrl=event_dlq_url)
    assert "Messages" not in dlq_messages or dlq_messages["Messages"] == []

    # now wait for the second invocation result which is expected to have processed message 2 and 3
    # Since we are re-queuing twice, with a visiblity timeout of 8s, this should instead be waiting for 20s => 8s x 2 retries (+ 4s margin).
    # See AWS docs: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ReceiveMessage.html#API_ReceiveMessage_RequestSyntax
    second_timeout_with_margin = (visibility_timeout * 2) + 4
    assert second_timeout_with_margin <= 20, (
        "An SQS ReceiveMessage operation cannot wait for more than 20s"
    )
    second_invocation = aws_client.sqs.receive_message(
        QueueUrl=destination_url, WaitTimeSeconds=second_timeout_with_margin, MaxNumberOfMessages=1
    )
    assert "Messages" in second_invocation
    # hack to make snapshot work
    second_invocation["Messages"][0]["Body"] = json.loads(second_invocation["Messages"][0]["Body"])
    second_invocation["Messages"][0]["Body"]["event"]["Records"].sort(
        key=lambda record: json.loads(record["body"])["message"]
    )
    snapshot.match("second_invocation", second_invocation)

    # here we make sure there's actually not a third attempt, since our retries = 2
    third_attempt = aws_client.sqs.receive_message(
        QueueUrl=destination_url, WaitTimeSeconds=1, MaxNumberOfMessages=1
    )
    assert "Messages" not in third_attempt or third_attempt["Messages"] == []

    # now check that message 4 was placed in the DLQ
    dlq_response = aws_client.sqs.receive_message(QueueUrl=event_dlq_url, WaitTimeSeconds=15)
    snapshot.match("dlq_response", dlq_response)


@markers.aws.validated
def test_report_batch_item_failures_on_lambda_error(
    create_lambda_function,
    sqs_create_queue,
    sqs_get_queue_arn,
    lambda_su_role,
    snapshot,
    cleanups,
    aws_client,
):
    # timeout in seconds, used for both the lambda and the queue visibility timeout
    retry_timeout = 2
    retries = 2

    # set up lambda function
    function_name = f"failing-lambda-{short_uid()}"
    create_lambda_function(
        func_name=function_name,
        handler_file=LAMBDA_SQS_INTEGRATION_FILE,
        runtime=Runtime.python3_12,
        role=lambda_su_role,
        timeout=retry_timeout,  # timeout needs to be <= than visibility timeout
    )

    # create dlq for event source queue
    event_dlq_url = sqs_create_queue(QueueName=f"event-dlq-{short_uid()}")
    event_dlq_arn = sqs_get_queue_arn(event_dlq_url)

    # create event source queue
    event_source_url = sqs_create_queue(
        QueueName=f"source-queue-{short_uid()}",
        Attributes={
            # the visibility timeout is implicitly also the time between retries
            "VisibilityTimeout": str(retry_timeout),
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": event_dlq_arn, "maxReceiveCount": retries}
            ),
        },
    )
    event_source_arn = sqs_get_queue_arn(event_source_url)

    # send a batch with a message to the queue that provokes a lambda failure (the lambda tries to parse the body as
    # JSON, but if it's not a json document, it fails). consequently, the entire batch should be discarded
    aws_client.sqs.send_message_batch(
        QueueUrl=event_source_url,
        Entries=[
            {
                "Id": "message-1",
                "MessageBody": "{not a json body",
            },
            {
                # this one's ok, but will be sent to the DLQ nonetheless because it's part of this bad batch.
                "Id": "message-2",
                "MessageBody": json.dumps({"message": 2, "fail_attempts": 0}),
            },
        ],
    )
    _await_queue_size(aws_client.sqs, event_source_url, qsize=2)

    # wire everything with the event source mapping
    mapping_uuid = aws_client.lambda_.create_event_source_mapping(
        EventSourceArn=event_source_arn,
        FunctionName=function_name,
        FunctionResponseTypes=["ReportBatchItemFailures"],
    )["UUID"]
    cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
    _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

    # the message should arrive in the DLQ after 2 retries + some time for processing

    messages = []

    def _collect_message():
        dlq_response = aws_client.sqs.receive_message(QueueUrl=event_dlq_url)
        messages.extend(dlq_response.get("Messages", []))
        assert len(messages) >= 2

    # the message should arrive in the DLQ after 2 retries + some time for processing
    wait_time = retry_timeout * retries
    retry(_collect_message, retries=10, sleep=1, sleep_before=wait_time)

    messages.sort(
        key=lambda m: m["MD5OfBody"]
    )  # otherwise the two messages are switched around sometimes (not deterministic)

    snapshot.match("dlq_messages", messages)


@markers.aws.validated
def test_report_batch_item_failures_invalid_result_json_batch_fails(
    create_lambda_function,
    sqs_create_queue,
    sqs_get_queue_arn,
    lambda_su_role,
    snapshot,
    cleanups,
    aws_client,
):
    # create queue used in the lambda to send invocation results to (to verify lambda was invoked)
    destination_queue_name = f"destination-queue-{short_uid()}"
    destination_url = sqs_create_queue(QueueName=destination_queue_name)
    snapshot.match(
        "get_destination_queue_url", aws_client.sqs.get_queue_url(QueueName=destination_queue_name)
    )

    # timeout in seconds, used for both the lambda and the queue visibility timeout.
    # increase to 10 if testing against AWS fails.
    retry_timeout = 4
    retries = 2

    # set up lambda function
    function_name = f"failing-lambda-{short_uid()}"
    create_lambda_function(
        func_name=function_name,
        handler_file=LAMBDA_SQS_BATCH_ITEM_FAILURE_FILE,
        runtime=Runtime.python3_12,
        role=lambda_su_role,
        timeout=retry_timeout,  # timeout needs to be <= than visibility timeout
        envvars={
            "DESTINATION_QUEUE_URL": destination_url,
            "OVERWRITE_RESULT": '{"batchItemFailures": [{"foo":"notvalid"}]}',
        },
    )

    # create dlq for event source queue
    event_dlq_url = sqs_create_queue(QueueName=f"event-dlq-{short_uid()}")
    event_dlq_arn = sqs_get_queue_arn(event_dlq_url)

    # create event source queue
    event_source_url = sqs_create_queue(
        QueueName=f"source-queue-{short_uid()}",
        Attributes={
            # the visibility timeout is implicitly also the time between retries
            "VisibilityTimeout": str(retry_timeout),
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": event_dlq_arn, "maxReceiveCount": retries}
            ),
        },
    )
    event_source_arn = sqs_get_queue_arn(event_source_url)

    # wire everything with the event source mapping
    mapping_uuid = aws_client.lambda_.create_event_source_mapping(
        EventSourceArn=event_source_arn,
        FunctionName=function_name,
        BatchSize=10,
        MaximumBatchingWindowInSeconds=0,
        FunctionResponseTypes=["ReportBatchItemFailures"],
    )["UUID"]
    cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
    _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

    # trigger the lambda, the message content doesn't matter because the whole batch should be treated as failure
    aws_client.sqs.send_message(
        QueueUrl=event_source_url,
        MessageBody=json.dumps({"message": 1, "fail_attempts": 0}),
    )

    # now wait for the first invocation result which is expected to have processed message 1 we wait half the retry
    # interval to wait long enough for the message to appear, but short enough to check that the DLQ is empty after
    # the first attempt.
    first_invocation = aws_client.sqs.receive_message(
        QueueUrl=destination_url, WaitTimeSeconds=15, MaxNumberOfMessages=1
    )
    assert "Messages" in first_invocation
    snapshot.match("first_invocation", first_invocation)

    # now wait for the second invocation result, which should be a retry of the first
    second_invocation = aws_client.sqs.receive_message(
        QueueUrl=destination_url, WaitTimeSeconds=15, MaxNumberOfMessages=1
    )
    assert "Messages" in second_invocation
    # hack to make snapshot work
    snapshot.match("second_invocation", second_invocation)

    # now check that the messages was placed in the DLQ
    dlq_response = aws_client.sqs.receive_message(QueueUrl=event_dlq_url, WaitTimeSeconds=15)
    assert "Messages" in dlq_response
    snapshot.match("dlq_response", dlq_response)


@markers.aws.validated
def test_report_batch_item_failures_empty_json_batch_succeeds(
    create_lambda_function,
    sqs_create_queue,
    sqs_get_queue_arn,
    lambda_su_role,
    snapshot,
    cleanups,
    aws_client,
):
    # create queue used in the lambda to send invocation results to (to verify lambda was invoked)
    destination_queue_name = f"destination-queue-{short_uid()}"
    destination_url = sqs_create_queue(QueueName=destination_queue_name)
    snapshot.match(
        "get_destination_queue_url", aws_client.sqs.get_queue_url(QueueName=destination_queue_name)
    )

    # timeout in seconds, used for both the lambda and the queue visibility timeout.
    # increase to 10 if testing against AWS fails.
    retry_timeout = 4
    retries = 1

    # set up lambda function
    function_name = f"failing-lambda-{short_uid()}"
    create_lambda_function(
        func_name=function_name,
        handler_file=LAMBDA_SQS_BATCH_ITEM_FAILURE_FILE,
        runtime=Runtime.python3_12,
        role=lambda_su_role,
        timeout=retry_timeout,  # timeout needs to be <= than visibility timeout
        envvars={"DESTINATION_QUEUE_URL": destination_url, "OVERWRITE_RESULT": "{}"},
    )

    # create dlq for event source queue
    event_dlq_url = sqs_create_queue(QueueName=f"event-dlq-{short_uid()}")
    event_dlq_arn = sqs_get_queue_arn(event_dlq_url)

    # create event source queue
    # we use a FIFO queue to be sure the lambda is invoked in a deterministic way
    event_source_url = sqs_create_queue(
        QueueName=f"source-queue-{short_uid()}",
        Attributes={
            # the visibility timeout is implicitly also the time between retries
            "VisibilityTimeout": str(retry_timeout),
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": event_dlq_arn, "maxReceiveCount": retries}
            ),
        },
    )
    event_source_arn = sqs_get_queue_arn(event_source_url)

    # wire everything with the event source mapping
    mapping_uuid = aws_client.lambda_.create_event_source_mapping(
        EventSourceArn=event_source_arn,
        FunctionName=function_name,
        BatchSize=10,
        MaximumBatchingWindowInSeconds=0,
        FunctionResponseTypes=["ReportBatchItemFailures"],
    )["UUID"]
    cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
    _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

    # trigger the lambda, the message content doesn't matter because the whole batch should be treated as failure
    aws_client.sqs.send_message(
        QueueUrl=event_source_url,
        MessageBody=json.dumps({"message": 1, "fail_attempts": 0}),
    )

    # now wait for the first invocation result which is expected to have processed message 1 we wait half the retry
    # interval to wait long enough for the message to appear, but short enough to check that the DLQ is empty after
    # the first attempt.
    first_invocation = aws_client.sqs.receive_message(
        QueueUrl=destination_url, WaitTimeSeconds=15, MaxNumberOfMessages=1
    )
    snapshot.match("first_invocation", first_invocation)

    # now check that the messages was placed in the DLQ
    dlq_response = aws_client.sqs.receive_message(
        QueueUrl=event_dlq_url, WaitTimeSeconds=retry_timeout + 1
    )
    assert "Messages" not in dlq_response or dlq_response["Messages"] == []


@markers.aws.validated
def test_fifo_message_group_parallelism(
    aws_client,
    create_lambda_function,
    lambda_su_role,
    cleanups,
    snapshot,
):
    # https://github.com/localstack/localstack/issues/7036
    lambda_client = aws_client.lambda_
    logs_client = aws_client.logs

    # create FIFO queue
    queue_name = f"test-queue-{short_uid()}.fifo"
    create_queue_result = aws_client.sqs.create_queue(
        QueueName=queue_name,
        Attributes={
            "FifoQueue": "true",
            "ContentBasedDeduplication": "true",
            "VisibilityTimeout": "60",
        },
    )
    queue_url = create_queue_result["QueueUrl"]
    queue_arn = aws_client.sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]

    message_group_id = "fixed-message-group-id-test"

    # create a lambda to process messages
    function_name = f"function-name-{short_uid()}"

    create_lambda_function(
        func_name=function_name,
        handler_file=LAMBDA_SLEEP_FILE,
        runtime=Runtime.python3_12,
        role=lambda_su_role,
        timeout=10,
        Environment={"Variables": {"TEST_SLEEP_S": "5"}},
    )

    # create event source mapping
    create_esm_result = lambda_client.create_event_source_mapping(
        FunctionName=function_name, EventSourceArn=queue_arn, Enabled=False, BatchSize=1
    )
    snapshot.match("create_esm_disabled", create_esm_result)
    esm_uuid = create_esm_result["UUID"]
    cleanups.append(lambda: lambda_client.delete_event_source_mapping(UUID=esm_uuid))

    # send messages
    for i in range(5):
        aws_client.sqs.send_message(
            QueueUrl=queue_url, MessageBody=f"message-{i}", MessageGroupId=message_group_id
        )

    # enable event source mapping
    update_esm_enabling = lambda_client.update_event_source_mapping(UUID=esm_uuid, Enabled=True)
    snapshot.match("update_esm_enabling", update_esm_enabling)
    _await_event_source_mapping_enabled(lambda_client, esm_uuid)
    get_esm_enabled = lambda_client.get_event_source_mapping(UUID=esm_uuid)
    snapshot.match("get_esm_enabled", get_esm_enabled)

    # since the lambda has to be called in-order anyway, there shouldn't be any parallel executions
    log_group_name = f"/aws/lambda/{function_name}"

    time.sleep(60)

    log_streams = logs_client.describe_log_streams(logGroupName=log_group_name)
    assert len(log_streams["logStreams"]) == 1


@markers.snapshot.skip_snapshot_verify(
    paths=[
        # events attribute
        "$..Records..md5OfMessageAttributes",
    ],
)
class TestSQSEventSourceMapping:
    @markers.aws.validated
    def test_event_source_mapping_default_batch_size(
        self,
        create_lambda_function,
        create_event_source_mapping,
        sqs_create_queue,
        sqs_get_queue_arn,
        lambda_su_role,
        snapshot,
        aws_client,
    ):
        snapshot.add_transformer(snapshot.transform.lambda_api())
        function_name = f"lambda_func-{short_uid()}"
        queue_name_1 = f"queue-{short_uid()}-1"
        queue_name_2 = f"queue-{short_uid()}-2"
        queue_url_1 = sqs_create_queue(QueueName=queue_name_1)
        queue_arn_1 = sqs_get_queue_arn(queue_url_1)

        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            runtime=Runtime.python3_12,
            role=lambda_su_role,
        )

        rs = create_event_source_mapping(EventSourceArn=queue_arn_1, FunctionName=function_name)
        snapshot.match("create-event-source-mapping", rs)

        uuid = rs["UUID"]
        assert DEFAULT_SQS_BATCH_SIZE == rs["BatchSize"]
        _await_event_source_mapping_enabled(aws_client.lambda_, uuid)

        with pytest.raises(ClientError) as e:
            # Update batch size with invalid value
            rs = aws_client.lambda_.update_event_source_mapping(
                UUID=uuid,
                FunctionName=function_name,
                BatchSize=MAX_SQS_BATCH_SIZE_FIFO + 1,
            )
        snapshot.match("invalid-update-event-source-mapping", e.value.response)
        e.match(InvalidParameterValueException.code)

        queue_url_2 = sqs_create_queue(QueueName=queue_name_2)
        queue_arn_2 = sqs_get_queue_arn(queue_url_2)

        with pytest.raises(ClientError) as e:
            # Create event source mapping with invalid batch size value
            rs = aws_client.lambda_.create_event_source_mapping(
                EventSourceArn=queue_arn_2,
                FunctionName=function_name,
                BatchSize=MAX_SQS_BATCH_SIZE_FIFO + 1,
            )
        snapshot.match("invalid-create-event-source-mapping", e.value.response)
        e.match(InvalidParameterValueException.code)

    @markers.aws.validated
    def test_sqs_event_source_mapping(
        self,
        create_lambda_function,
        sqs_create_queue,
        sqs_get_queue_arn,
        lambda_su_role,
        snapshot,
        cleanups,
        aws_client,
    ):
        function_name = f"lambda_func-{short_uid()}"
        queue_name_1 = f"queue-{short_uid()}-1"
        mapping_uuid = None

        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            runtime=Runtime.python3_12,
            role=lambda_su_role,
        )
        queue_url_1 = sqs_create_queue(QueueName=queue_name_1)
        queue_arn_1 = sqs_get_queue_arn(queue_url_1)
        create_event_source_mapping_response = aws_client.lambda_.create_event_source_mapping(
            EventSourceArn=queue_arn_1,
            FunctionName=function_name,
            MaximumBatchingWindowInSeconds=1,
        )
        mapping_uuid = create_event_source_mapping_response["UUID"]
        cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
        snapshot.match("create-event-source-mapping-response", create_event_source_mapping_response)
        _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

        aws_client.sqs.send_message(QueueUrl=queue_url_1, MessageBody=json.dumps({"foo": "bar"}))

        events = retry(
            check_expected_lambda_log_events_length,
            retries=10,
            sleep=1,
            function_name=function_name,
            expected_length=1,
            logs_client=aws_client.logs,
        )
        snapshot.match("events", events)

        rs = aws_client.sqs.receive_message(QueueUrl=queue_url_1)
        assert rs.get("Messages", []) == []

    @pytest.mark.parametrize("batch_size", [15, 100, 1_000, 10_000])
    @markers.aws.validated
    def test_sqs_event_source_mapping_batch_size(
        self,
        create_lambda_function,
        sqs_create_queue,
        sqs_get_queue_arn,
        lambda_su_role,
        snapshot,
        cleanups,
        aws_client,
        batch_size,
    ):
        snapshot.add_transformer(snapshot.transform.sqs_api())
        snapshot.add_transformer(SortingTransformer("Records", lambda s: s["body"]), priority=-1)
        # Intentional parity difference to speed up testing in LocalStack
        snapshot.add_transformer(
            snapshot.transform.key_value(
                "MaximumBatchingWindowInSeconds", reference_replacement=False
            )
        )

        destination_queue_name = f"destination-queue-{short_uid()}"
        function_name = f"lambda_func-{short_uid()}"
        source_queue_name = f"source-queue-{short_uid()}"
        mapping_uuid = None

        destination_queue_url = sqs_create_queue(QueueName=destination_queue_name)
        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_EVENT_SOURCE_MAPPING_SEND_MESSAGE,
            runtime=Runtime.python3_12,
            envvars={"SQS_QUEUE_URL": destination_queue_url},
            role=lambda_su_role,
        )

        queue_url = sqs_create_queue(QueueName=source_queue_name)
        queue_arn = sqs_get_queue_arn(queue_url)

        create_event_source_mapping_response = aws_client.lambda_.create_event_source_mapping(
            EventSourceArn=queue_arn,
            FunctionName=function_name,
            # Speed up testing in LocalStack by waiting only up to 2s instead of up to 10s; AWS is slower.
            MaximumBatchingWindowInSeconds=10 if is_aws_cloud() else 2,
            BatchSize=batch_size,
        )
        mapping_uuid = create_event_source_mapping_response["UUID"]
        cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
        snapshot.match("create-event-source-mapping-response", create_event_source_mapping_response)
        _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

        response_batch_send_10 = aws_client.sqs.send_message_batch(
            QueueUrl=queue_url,
            Entries=[{"Id": f"{i}-0", "MessageBody": f"{i}-0-message-{i}"} for i in range(10)],
        )
        snapshot.match("send-message-batch-result-10", response_batch_send_10)

        response_batch_send_5 = aws_client.sqs.send_message_batch(
            QueueUrl=queue_url,
            Entries=[{"Id": f"{i}-1", "MessageBody": f"{i}-1-message-{i}"} for i in range(5)],
        )
        snapshot.match("send-message-batch-result-5", response_batch_send_5)

        batches = []

        def get_msg_from_q():
            messages_to_delete = []
            receive_message_response = aws_client.sqs.receive_message(
                QueueUrl=destination_queue_url,
                MaxNumberOfMessages=10,
                VisibilityTimeout=120,
                WaitTimeSeconds=5 if is_aws_cloud() else 1,
            )
            messages = receive_message_response.get("Messages", [])
            for message in messages:
                received_batch = json.loads(message["Body"])
                batches.append(received_batch)
                messages_to_delete.append(
                    {"Id": message["MessageId"], "ReceiptHandle": message["ReceiptHandle"]}
                )
            if messages_to_delete:
                aws_client.sqs.delete_message_batch(
                    QueueUrl=destination_queue_url, Entries=messages_to_delete
                )
            assert sum([len(batch) for batch in batches]) == 15
            return [message for batch in batches for message in batch]

        events = retry(get_msg_from_q, retries=15, sleep=5)
        snapshot.match("Records", events)

    @markers.aws.validated
    def test_sqs_event_source_mapping_batching_reserved_concurrency(
        self,
        create_lambda_function,
        sqs_create_queue,
        sqs_get_queue_arn,
        lambda_su_role,
        snapshot,
        cleanups,
        aws_client,
    ):
        snapshot.add_transformer(snapshot.transform.sqs_api())
        snapshot.add_transformer(SortingTransformer("Records", lambda s: s["body"]), priority=-1)

        destination_queue_name = f"destination-queue-{short_uid()}"
        function_name = f"lambda_func-{short_uid()}"
        source_queue_name = f"source-queue-{short_uid()}"
        mapping_uuid = None

        destination_queue_url = sqs_create_queue(QueueName=destination_queue_name)
        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_EVENT_SOURCE_MAPPING_SEND_MESSAGE,
            runtime=Runtime.python3_12,
            envvars={"SQS_QUEUE_URL": destination_queue_url},
            role=lambda_su_role,
        )

        # Prevent more than 2 Lambdas from being spun up at a time
        put_concurrency_resp = aws_client.lambda_.put_function_concurrency(
            FunctionName=function_name, ReservedConcurrentExecutions=2
        )
        snapshot.match("put_concurrency_resp", put_concurrency_resp)

        queue_url = sqs_create_queue(QueueName=source_queue_name)
        queue_arn = sqs_get_queue_arn(queue_url)

        for b in range(3):
            aws_client.sqs.send_message_batch(
                QueueUrl=queue_url,
                Entries=[{"Id": f"{i}-{b}", "MessageBody": f"{i}-{b}-message"} for i in range(10)],
            )

        create_event_source_mapping_response = aws_client.lambda_.create_event_source_mapping(
            EventSourceArn=queue_arn,
            FunctionName=function_name,
            MaximumBatchingWindowInSeconds=1,
            BatchSize=20,
            ScalingConfig={
                "MaximumConcurrency": 2
            },  # Prevent more than 2 concurrent SQS workers from being spun up at a time
        )
        mapping_uuid = create_event_source_mapping_response["UUID"]
        cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
        snapshot.match("create-event-source-mapping-response", create_event_source_mapping_response)
        _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

        batches = []

        def get_msg_from_q():
            messages_to_delete = []
            receive_message_response = aws_client.sqs.receive_message(
                QueueUrl=destination_queue_url,
                MaxNumberOfMessages=10,
                VisibilityTimeout=120,
                WaitTimeSeconds=5,
            )
            messages = receive_message_response.get("Messages", [])
            for message in messages:
                received_batch = json.loads(message["Body"])
                batches.append(received_batch)
                messages_to_delete.append(
                    {"Id": message["MessageId"], "ReceiptHandle": message["ReceiptHandle"]}
                )

            if messages_to_delete:
                aws_client.sqs.delete_message_batch(
                    QueueUrl=destination_queue_url, Entries=messages_to_delete
                )
            assert sum([len(batch) for batch in batches]) == 30
            return [message for batch in batches for message in batch]

        events = retry(get_msg_from_q, retries=15, sleep=5)

        # We expect to receive 2 batches where each batch contains some proportion of the
        # 30 messages we sent through, divided by the 20 ESM batch size. How this is split is
        # not determinable a priori so rather just snapshots the events and the no. of batches.
        snapshot.match("batch_info", {"total_batches_received": len(batches)})
        snapshot.match("Records", events)

    @markers.aws.validated
    @pytest.mark.parametrize(
        # EventBridge event pattern filtering test suite: tests/aws/services/events/test_events_patterns.py
        # Event filtering: https://docs.aws.amazon.com/lambda/latest/dg/invocation-eventfiltering.html
        # Special cases behavior: https://docs.aws.amazon.com/lambda/latest/dg/with-sqs-filtering.html
        "filter, item_matching, item_not_matching",
        [
            # test single filter
            pytest.param(
                {"body": {"my-key": ["my-value"]}},
                {"my-key": "my-value"},
                {"my-key": "other-value"},
                id="single",
            ),
            # test OR filter
            pytest.param(
                {"body": {"my-key": ["my-value-one", "my-value-two"]}},
                {"my-key": "my-value-two"},
                {"my-key": "other-value"},
                id="or",
            ),
            # test AND filter
            pytest.param(
                {
                    "body": {
                        "my-key-one": ["other-filter", "my-value-one"],
                        "my-key-two": ["my-value-two"],
                    }
                },
                {"my-key-one": "my-value-one", "my-key-two": "my-value-two"},
                {"my-key-one": "other-value-", "my-key-two": "my-value-two"},
                id="and",
            ),
            # exists
            pytest.param(
                {"body": {"my-key": [{"exists": True}]}},
                {"my-key": "any-value-one"},
                {"other-key": "any-value-two"},
                id="exists",
            ),
            # numeric (bigger)
            pytest.param(
                {"body": {"my-number": [{"numeric": [">", 100]}]}},
                {"my-number": 101},
                {"my-number": 100},
                id="numeric-bigger",
            ),
            # numeric (smaller)
            pytest.param(
                {"body": {"my-number": [{"numeric": ["<", 100]}]}},
                {"my-number": 99},
                {"my-number": 100},
                id="numeric-smaller",
            ),
            # numeric (range)
            pytest.param(
                {"body": {"my-number": [{"numeric": [">=", 100, "<", 200]}]}},
                {"my-number": 100},
                {"my-number": 200},
                id="numeric-range",
            ),
            # prefix
            pytest.param(
                {"body": {"my-key": [{"prefix": "yes"}]}},
                {"my-key": "yes-value"},
                {"my-key": "no-value"},
                id="prefix",
            ),
            # plain string matching
            # TODO: How is plain string matching supposed to work?
            #  https://docs.aws.amazon.com/lambda/latest/dg/with-sqs-filtering.html
            pytest.param(
                {"body": "plain-string"},
                "plain-string",
                "plain-string-not-matching",
                id="plain-string-matching",
                marks=pytest.mark.skip(reason="figure out how plain string matching works"),
            ),
            # plain string filter
            # TODO: How is plain string matching supposed to work?
            #  https://docs.aws.amazon.com/lambda/latest/dg/with-sqs-filtering.html
            pytest.param(
                {"body": "plain-string"},
                "plain-string",
                # valid json body vs. plain string filter for body -> drop the message
                {"valid-json-key": "plain-string"},
                id="plain-string-filter",
                marks=pytest.mark.skip(reason="figure out how plain string matching works"),
            ),
            # valid json filter
            pytest.param(
                {"body": {"my-key": ["my-value"]}},
                {"my-key": "my-value"},
                # plain string body vs. valid json filter for body -> drop the message
                "plain-string",
                id="valid-json-filter",
            ),
        ],
    )
    def test_sqs_event_filter(
        self,
        create_lambda_function,
        sqs_create_queue,
        sqs_get_queue_arn,
        lambda_su_role,
        filter,
        item_matching,
        item_not_matching,
        snapshot,
        cleanups,
        aws_client,
        monkeypatch,
    ):
        function_name = f"lambda_func-{short_uid()}"
        queue_name_1 = f"queue-{short_uid()}-1"
        mapping_uuid = None

        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            runtime=Runtime.python3_12,
            role=lambda_su_role,
        )
        queue_url_1 = sqs_create_queue(QueueName=queue_name_1)
        queue_arn_1 = sqs_get_queue_arn(queue_url_1)

        aws_client.sqs.send_message(QueueUrl=queue_url_1, MessageBody=json.dumps(item_matching))
        aws_client.sqs.send_message(
            QueueUrl=queue_url_1,
            MessageBody=json.dumps(item_not_matching)
            if not isinstance(item_not_matching, str)
            else item_not_matching,
        )

        def _assert_qsize():
            response = aws_client.sqs.get_queue_attributes(
                QueueUrl=queue_url_1, AttributeNames=["ApproximateNumberOfMessages"]
            )
            assert int(response["Attributes"]["ApproximateNumberOfMessages"]) == 2

        retry(_assert_qsize, retries=10)

        create_event_source_mapping_response = aws_client.lambda_.create_event_source_mapping(
            EventSourceArn=queue_arn_1,
            FunctionName=function_name,
            MaximumBatchingWindowInSeconds=1,
            FilterCriteria={
                "Filters": [
                    {"Pattern": json.dumps(filter)},
                ]
            },
        )
        mapping_uuid = create_event_source_mapping_response["UUID"]
        cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
        snapshot.match("create_event_source_mapping_response", create_event_source_mapping_response)
        _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

        def _check_lambda_logs():
            events = get_lambda_log_events(function_name, logs_client=aws_client.logs)
            # once invoked
            assert len(events) == 1
            records = events[0]["Records"]
            # one record processed
            assert len(records) == 1
            # check for correct record presence
            if "body" in json.dumps(filter):
                item_matching_str = json.dumps(item_matching)
                assert records[0]["body"] == item_matching_str
            return events

        invocation_events = retry(_check_lambda_logs, retries=10)
        snapshot.match("invocation_events", invocation_events)

        rs = aws_client.sqs.receive_message(QueueUrl=queue_url_1)
        assert rs.get("Messages", []) == []

    @markers.aws.validated
    @pytest.mark.parametrize(
        "invalid_filter", [None, "simple string", {"eventSource": "aws:sqs"}, {"eventSource": []}]
    )
    def test_sqs_invalid_event_filter(
        self,
        create_lambda_function,
        sqs_create_queue,
        sqs_get_queue_arn,
        lambda_su_role,
        invalid_filter,
        snapshot,
        aws_client,
    ):
        function_name = f"lambda_func-{short_uid()}"
        queue_name_1 = f"queue-{short_uid()}"

        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            runtime=Runtime.python3_12,
            role=lambda_su_role,
        )
        queue_url_1 = sqs_create_queue(QueueName=queue_name_1)
        queue_arn_1 = sqs_get_queue_arn(queue_url_1)

        with pytest.raises(ClientError) as expected:
            aws_client.lambda_.create_event_source_mapping(
                EventSourceArn=queue_arn_1,
                FunctionName=function_name,
                MaximumBatchingWindowInSeconds=1,
                FilterCriteria={
                    "Filters": [
                        {
                            "Pattern": invalid_filter
                            if isinstance(invalid_filter, str)
                            else json.dumps(invalid_filter)
                        },
                    ]
                },
            )
        snapshot.match("create_event_source_mapping_exception", expected.value.response)
        expected.match(InvalidParameterValueException.code)

    @markers.aws.validated
    def test_sqs_event_source_mapping_update(
        self,
        create_lambda_function,
        sqs_create_queue,
        sqs_get_queue_arn,
        lambda_su_role,
        snapshot,
        cleanups,
        aws_client,
    ):
        """
        Testing an update to an event source mapping that changes the targeted lambda function version

        Resources used:
        - Lambda function
        - 2 published versions of that lambda function
        - 1 event source mapping

        First the event source mapping points towards the qualified ARN of the first version.
        A message is sent to the SQS queue, triggering the function version with ID 1.
        The lambda function is updated with a different value for the environment variable and a new version published.
        Then we update the event source mapping and make the qualified ARN of the function version with ID 2 the new target.
        A message is sent to the SQS queue, triggering the function with version ID 2.

        We should have one log entry for each of the invocations.

        """
        function_name = f"lambda_func-{short_uid()}"
        queue_name_1 = f"queue-{short_uid()}-1"
        mapping_uuid = None

        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO_VERSION_ENV,
            runtime=Runtime.python3_12,
            role=lambda_su_role,
        )

        aws_client.lambda_.update_function_configuration(
            FunctionName=function_name, Environment={"Variables": {"CUSTOM_VAR": "a"}}
        )
        aws_client.lambda_.get_waiter("function_updated_v2").wait(FunctionName=function_name)
        publish_v1 = aws_client.lambda_.publish_version(FunctionName=function_name)
        aws_client.lambda_.get_waiter("function_active_v2").wait(
            FunctionName=publish_v1["FunctionArn"]
        )

        queue_url_1 = sqs_create_queue(QueueName=queue_name_1)
        queue_arn_1 = sqs_get_queue_arn(queue_url_1)
        create_event_source_mapping_response = aws_client.lambda_.create_event_source_mapping(
            EventSourceArn=queue_arn_1,
            FunctionName=publish_v1["FunctionArn"],
            MaximumBatchingWindowInSeconds=1,
        )
        mapping_uuid = create_event_source_mapping_response["UUID"]
        cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
        snapshot.match("create-event-source-mapping-response", create_event_source_mapping_response)
        _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

        aws_client.sqs.send_message(QueueUrl=queue_url_1, MessageBody=json.dumps({"foo": "bar"}))

        events = retry(
            check_expected_lambda_log_events_length,
            retries=10,
            sleep=1,
            function_name=function_name,
            expected_length=1,
            logs_client=aws_client.logs,
        )
        snapshot.match("events", events)

        rs = aws_client.sqs.receive_message(QueueUrl=queue_url_1)
        assert rs.get("Messages", []) == []

        # # create new function version
        aws_client.lambda_.update_function_configuration(
            FunctionName=function_name, Environment={"Variables": {"CUSTOM_VAR": "b"}}
        )
        aws_client.lambda_.get_waiter("function_updated_v2").wait(FunctionName=function_name)
        publish_v2 = aws_client.lambda_.publish_version(FunctionName=function_name)
        aws_client.lambda_.get_waiter("function_active_v2").wait(
            FunctionName=publish_v2["FunctionArn"]
        )
        # we're now pointing the existing event source mapping towards the new version.
        # only v2 should now be called
        updated_esm = aws_client.lambda_.update_event_source_mapping(
            UUID=mapping_uuid, FunctionName=publish_v2["FunctionArn"]
        )
        assert mapping_uuid == updated_esm["UUID"]
        assert publish_v2["FunctionArn"] == updated_esm["FunctionArn"]
        snapshot.match("updated_esm", updated_esm)
        _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

        # TODO: we actually would probably need to wait for an updating state here.
        #   we experience flaky cases on AWS where the next send actually goes to the old version.
        #   Not sure yet how we could prevent this
        if is_aws_cloud():
            time.sleep(10)

        # Verify that the ESM we get actually points to the latest version of Lambda function.
        get_updated_esm = aws_client.lambda_.get_event_source_mapping(UUID=mapping_uuid)
        snapshot.match("updated_event_source_mapping", get_updated_esm)

        # verify function v2 was called, not latest and not v1
        aws_client.sqs.send_message(QueueUrl=queue_url_1, MessageBody=json.dumps({"foo": "bar2"}))
        # get the event message
        events_postupdate = retry(
            check_expected_lambda_log_events_length,
            retries=10,
            sleep=1,
            function_name=function_name,
            expected_length=2,
            logs_client=aws_client.logs,
        )
        snapshot.match("events_postupdate", events_postupdate)

        rs = aws_client.sqs.receive_message(QueueUrl=queue_url_1)
        assert rs.get("Messages", []) == []

    @markers.aws.validated
    def test_duplicate_event_source_mappings(
        self,
        create_lambda_function,
        lambda_su_role,
        create_event_source_mapping,
        sqs_create_queue,
        sqs_get_queue_arn,
        snapshot,
        aws_client,
    ):
        function_name_1 = f"lambda_func-{short_uid()}"
        function_name_2 = f"lambda_func-{short_uid()}"

        event_source_arn = sqs_get_queue_arn(sqs_create_queue())

        create_lambda_function(
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            func_name=function_name_1,
            runtime=Runtime.python3_12,
            role=lambda_su_role,
        )

        response = create_event_source_mapping(
            FunctionName=function_name_1,
            EventSourceArn=event_source_arn,
        )
        snapshot.match("create", response)

        with pytest.raises(ClientError) as e:
            create_event_source_mapping(
                FunctionName=function_name_1,
                EventSourceArn=event_source_arn,
            )

        response = e.value.response
        snapshot.match("error", response)

        # this should work without problem since it's a new function
        create_lambda_function(
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            func_name=function_name_2,
            runtime=Runtime.python3_12,
            role=lambda_su_role,
        )
        create_event_source_mapping(
            FunctionName=function_name_2,
            EventSourceArn=event_source_arn,
        )

    @pytest.mark.parametrize(
        "batch_size",
        [
            20,
            100,
            1_000,
            pytest.param(
                10_000,
                id="10000",
                marks=pytest.mark.skipif(
                    condition=not is_env_true("TEST_PERFORMANCE"),
                    reason="Too resource intensive to be randomly allocated to a runner.",
                ),
            ),
        ],
    )
    @markers.aws.only_localstack
    def test_sqs_event_source_mapping_batch_size_override(
        self,
        create_lambda_function,
        sqs_create_queue,
        sqs_get_queue_arn,
        lambda_su_role,
        cleanups,
        aws_client,
        batch_size,
    ):
        function_name = f"lambda_func-{short_uid()}"
        queue_name = f"queue-{short_uid()}"
        mapping_uuid = None

        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            runtime=Runtime.python3_12,
            role=lambda_su_role,
        )
        queue_url = sqs_create_queue(QueueName=queue_name)
        queue_arn = sqs_get_queue_arn(queue_url)

        # Send messages in batches of 10 i.e batch_size = 10_000 means 1_000 requests of 10 messages each.
        for _ in range(batch_size // 10):
            entries = [{"Id": str(i), "MessageBody": json.dumps({"foo": "bar"})} for i in range(10)]
            aws_client.sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)

        # Wait a few seconds to ensure all messages are loaded in queue
        _await_queue_size(aws_client.sqs, queue_url, batch_size)

        create_event_source_mapping_response = aws_client.lambda_.create_event_source_mapping(
            EventSourceArn=queue_arn,
            FunctionName=function_name,
            MaximumBatchingWindowInSeconds=10,
            BatchSize=batch_size,
        )
        mapping_uuid = create_event_source_mapping_response["UUID"]
        cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
        _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

        expected_invocations = math.ceil(batch_size / 2500)
        events = retry(
            check_expected_lambda_log_events_length,
            retries=10,
            sleep=1,
            function_name=function_name,
            expected_length=expected_invocations,
            logs_client=aws_client.logs,
        )

        assert sum(len(event.get("Records", [])) for event in events) == batch_size

        rs = aws_client.sqs.receive_message(QueueUrl=queue_url)
        assert rs.get("Messages", []) == []

    @markers.aws.only_localstack
    def test_sqs_event_source_mapping_batching_window_size_override(
        self,
        create_lambda_function,
        sqs_create_queue,
        sqs_get_queue_arn,
        lambda_su_role,
        cleanups,
        aws_client,
    ):
        function_name = f"lambda_func-{short_uid()}"
        queue_name = f"queue-{short_uid()}"
        mapping_uuid = None

        create_lambda_function(
            func_name=function_name,
            handler_file=TEST_LAMBDA_PYTHON_ECHO,
            runtime=Runtime.python3_12,
            role=lambda_su_role,
        )
        queue_url = sqs_create_queue(QueueName=queue_name)
        queue_arn = sqs_get_queue_arn(queue_url)

        create_event_source_mapping_response = aws_client.lambda_.create_event_source_mapping(
            EventSourceArn=queue_arn,
            FunctionName=function_name,
            MaximumBatchingWindowInSeconds=30,
            BatchSize=10_000,
        )
        mapping_uuid = create_event_source_mapping_response["UUID"]
        cleanups.append(lambda: aws_client.lambda_.delete_event_source_mapping(UUID=mapping_uuid))
        _await_event_source_mapping_enabled(aws_client.lambda_, mapping_uuid)

        # Send 4 messages and delay their arrival by 5, 10, 15, and 25 seconds respectively
        for s in [5, 10, 15, 25]:
            aws_client.sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps({"delayed": f"{s}"}),
            )

        events = retry(
            check_expected_lambda_log_events_length,
            retries=60,
            sleep=1,
            function_name=function_name,
            expected_length=1,
            logs_client=aws_client.logs,
        )

        assert len(events) == 1
        assert len(events[0].get("Records", [])) == 4

        rs = aws_client.sqs.receive_message(QueueUrl=queue_url)
        assert rs.get("Messages", []) == []


def _await_queue_size(sqs_client, queue_url: str, qsize: int, retries=10, sleep=1):
    # wait for all items to appear in the queue
    def _verify_event_queue_size():
        attr = "ApproximateNumberOfMessages"
        _approx = int(
            sqs_client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=[attr])[
                "Attributes"
            ][attr]
        )
        assert _approx >= qsize

    retry(_verify_event_queue_size, retries=retries, sleep=sleep)
