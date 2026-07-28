import json
import os

import boto3

_ecs = boto3.client("ecs")


def handler(event: dict, context) -> dict:
    for record in event["Records"]:
        body = json.loads(record["body"])
        _ecs.run_task(
            cluster=os.environ["ECS_CLUSTER"],
            taskDefinition=os.environ["ECS_TASK_DEFINITION"],
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": os.environ["SUBNET_IDS"].split(","),
                    "securityGroups": [os.environ["SECURITY_GROUP_ID"]],
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": "worker",
                        "environment": [
                            {"name": str(k).upper(), "value": str(v)}
                            for k, v in body.items() if v is not None
                        ],
                    }
                ]
            },
        )
    return {"statusCode": 200}
