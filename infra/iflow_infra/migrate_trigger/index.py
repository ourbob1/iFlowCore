"""
Deploy-time database migrations (a CDK trigger, runs inside CloudFormation before the API service
updates). Starts the one-off "migrate" ECS task (`alembic upgrade head`, same image as the API) and
waits for it; a failed migration fails the deployment, so a new image never serves an old schema.

It re-runs whenever the image changes (the image URI is in this function's environment, so a new
image means a new function version, which re-triggers). Nothing about users is read or logged:
only task ids and the exit code.
"""

from __future__ import annotations

import json
import os
import time

import boto3

ecs = boto3.client("ecs")
POLL_S = 10


def handler(event, context):
    subnets = json.loads(os.environ["SUBNETS"])
    response = ecs.run_task(
        cluster=os.environ["CLUSTER"], taskDefinition=os.environ["TASK_DEFINITION"], launchType="FARGATE",
        count=1, startedBy="iflow-migrate",
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": subnets, "securityGroups": [os.environ["SECURITY_GROUP"]], "assignPublicIp": "ENABLED"}},
    )
    if response.get("failures") or not response.get("tasks"):
        raise RuntimeError(f"Couldn't start the migration task: {response.get('failures')}")
    task_arn = response["tasks"][0]["taskArn"]
    print(f"migration task {task_arn.rsplit('/', 1)[-1]} started for image {os.environ['IMAGE']}")

    while True:                                   # Lambda's own timeout bounds this loop
        task = ecs.describe_tasks(cluster=os.environ["CLUSTER"], tasks=[task_arn])["tasks"][0]
        if task["lastStatus"] == "STOPPED":
            break
        time.sleep(POLL_S)
    container = next(c for c in task["containers"] if c["name"] == "migrate")
    code = container.get("exitCode")
    if code != 0:
        reason = container.get("reason") or task.get("stoppedReason") or "unknown"
        raise RuntimeError(f"Migration failed (exit code {code}): {reason}. See the /iflow-core/api/migrate logs.")
    print("migrations applied")
    return {"status": "migrated", "task": task_arn.rsplit("/", 1)[-1]}
