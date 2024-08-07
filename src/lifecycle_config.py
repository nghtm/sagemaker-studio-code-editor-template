import boto3
import base64
import json
import logging
import time
import cfnresponse

logger = logging.getLogger()
logger.setLevel(logging.INFO)
sagemaker = boto3.client("sagemaker")

AUTO_STOP_COMMAND_TEMPLATE = """\
ASI_VERSION=0.3.1

# System variables [do not change if not needed]
CONDA_HOME=/opt/conda/bin
LOG_FILE=/var/log/apps/app_container.log # Writing to app_container.log delivers logs to CW logs.
SOLUTION_DIR=/var/tmp/auto-stop-idle # Do not use /home/sagemaker-user
PYTHON_PACKAGE=sagemaker_code_editor_auto_shut_down-$ASI_VERSION.tar.gz
PYTHON_SCRIPT_PATH=$SOLUTION_DIR/sagemaker_code_editor_auto_shut_down/auto_stop_idle.py

# Installing cron
sudo apt-get update -y
sudo apt-get install -y -q vim

# Issue - https://github.com/aws-samples/sagemaker-studio-apps-lifecycle-config-examples/issues/12
# SM Distribution image 1.6 is not starting cron service by default https://github.com/aws/sagemaker-distribution/issues/354

# Check if cron needs to be installed
status="$(dpkg-query -W --showformat='${db:Status-Status}' "cron" 2>&1)"
if [ ! $? = 0 ] || [ ! "$status" = installed ]; then
    # Fixing invoke-rc.d: policy-rc.d denied execution of restart.
    sudo /bin/bash -c "echo '#!/bin/sh
    exit 0' > /usr/sbin/policy-rc.d"

    # Installing cron.
    echo "Installing cron..."
    sudo apt install cron
else
    echo "Package cron is already installed."
    sudo cron
fi

# Creating solution directory.
sudo mkdir -p $SOLUTION_DIR

# Downloading autostop idle Python package.
echo "Downloading autostop idle Python package..."
curl -LO --output-dir /var/tmp/ https://github.com/aws-samples/sagemaker-studio-apps-lifecycle-config-examples/releases/download/v$ASI_VERSION/$PYTHON_PACKAGE
sudo $CONDA_HOME/pip install -U -t $SOLUTION_DIR /var/tmp/$PYTHON_PACKAGE

# Touch file to ensure idleness timer is reset to 0
echo "Touching file to reset idleness timer"
touch /opt/amazon/sagemaker/sagemaker-code-editor-server-data/data/User/History/startup_timestamp

# Setting container credential URI variable to /etc/environment to make it available to cron
sudo /bin/bash -c "echo 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI=$AWS_CONTAINER_CREDENTIALS_RELATIVE_URI' >> /etc/environment"

# Add script to crontab for root.
echo "Adding autostop idle Python script to crontab..."
echo "*/2 * * * * /bin/bash -ic '$CONDA_HOME/python $PYTHON_SCRIPT_PATH --time __AUTO_STOP_IDLE_TIME__ --region $AWS_DEFAULT_REGION >> $LOG_FILE'" | sudo crontab -
"""

LCC_TEMPLATE = """\
#!/bin/bash
set -eux
echo 'debconf debconf/frontend select Noninteractive' | sudo debconf-set-selections
sudo apt update -qq && sudo apt upgrade -y -qq

# install php
sudo apt install -y -qq software-properties-common ca-certificates lsb-release apt-transport-https
LC_ALL=C.UTF-8 sudo add-apt-repository -y ppa:ondrej/php
sudo apt update -qq
sudo apt install -y -qq php8.2 php8.2-cli php8.2-common php8.2-fpm php8.2-mysql php8.2-zip php8.2-gd php8.2-mbstring php8.2-curl php8.2-xml php8.2-bcmath
sudo apt install -y -qq sqlite3 mysql-server

# install java
wget -O - https://apt.corretto.aws/corretto.key | sudo gpg --dearmor -o /usr/share/keyrings/corretto-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/corretto-keyring.gpg] https://apt.corretto.aws stable main" | sudo tee /etc/apt/sources.list.d/corretto.list
sudo apt update -qq
sudo apt install -y -qq java-17-amazon-corretto-jdk

# install docker
sudo apt update -qq
sudo apt install -y -qq ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
"deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
$(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update -qq
sudo apt install -y -qq docker-ce docker-ce-cli docker-buildx-plugin docker-compose-plugin
"""


def lambda_handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")
    domain_id = event["ResourceProperties"]["DomainId"]
    lifecycle_config_name = event["ResourceProperties"]["LifecycleConfigName"].lower()
    idle_mins = int(event["ResourceProperties"]["AutoStopIdleTimeInMinutes"])
    request_type = event["RequestType"]
    physical_resource_id = f"{domain_id}_{str(idle_mins)}"

    try:
        # create
        if request_type == "Create":
            res = create_lifecycle_config(lifecycle_config_name, idle_mins)
            update_domain(domain_id)
            logger.info(f"Studio Lifecycle Config '{lifecycle_config_name}' has been created.")
            send_success(event, context, {"Arn": res["StudioLifecycleConfigArn"]}, physical_resource_id)

        # update
        elif request_type == "Update":
            # fail if AutoStopIdleTimeInMinutes has been updated
            if physical_resource_id != event["PhysicalResourceId"]:
                raise ValueError(
                    "The update of 'AutoStopIdleTimeInMinutes' is not supported. Please recreate the stack instead."
                )
            res = sagemaker.describe_studio_lifecycle_config(StudioLifecycleConfigName=lifecycle_config_name)
            logger.info(f"Studio Lifecycle Config '{lifecycle_config_name}' has been updated.")
            send_success(event, context, {"Arn": res["StudioLifecycleConfigArn"]}, physical_resource_id)

        # delete
        elif request_type == "Delete":
            # check existing resources
            lcs = sagemaker.list_studio_lifecycle_configs(AppTypeEquals="CodeEditor")["StudioLifecycleConfigs"]
            lifecycle_config_names = [lc["StudioLifecycleConfigName"] for lc in lcs]
            # skip if lifecycle config has already been deleted
            if lifecycle_config_name in lifecycle_config_names:
                delete_lifecycle_config(lifecycle_config_name)
            logger.info(f"Studio Lifecycle Config '{lifecycle_config_name}' has been deleted.")
            send_success(event, context, {}, physical_resource_id)

    except Exception as e:
        send_failure(event, context, e)


def send_failure(event, context, e):
    logger.error(e)
    cfnresponse.send(event, context, cfnresponse.FAILED, {"Error": str(e)}, event.get("PhysicalResourceId"), reason=str(e))


def send_success(event, context, data, physical_resource_id):
    cfnresponse.send(event, context, cfnresponse.SUCCESS, data, physical_resource_id)


def create_lifecycle_config(lifecycle_config_name: str, idle_mins: int):
    lcc = LCC_TEMPLATE
    if idle_mins > 0:
        lcc += "\n" + AUTO_STOP_COMMAND_TEMPLATE.replace("__AUTO_STOP_IDLE_TIME__", str(60 * idle_mins))
    return sagemaker.create_studio_lifecycle_config(
        StudioLifecycleConfigName=lifecycle_config_name,
        StudioLifecycleConfigContent=base64.b64encode(lcc.encode("utf-8")).decode("utf-8"),
        StudioLifecycleConfigAppType="CodeEditor",
    )


def delete_lifecycle_config(lifecycle_config_name):
    try:
        sagemaker.delete_studio_lifecycle_config(StudioLifecycleConfigName=lifecycle_config_name)
    except sagemaker.exceptions.ResourceNotFound as e:
        logger.info(
            f"Studio Lifecycle Config '{lifecycle_config_name}' has beed deleted. Recovering from exception: {str(e)}"
        )
    except Exception as e:
        raise e


def update_domain(domain_id):
    sagemaker.update_domain(
        DomainId=domain_id,
        DomainSettingsForUpdate={
            "DockerSettings": {
                "EnableDockerAccess": "ENABLED",
            },
        },
    )
    while True:
        res = sagemaker.describe_domain(DomainId=domain_id)
        status = res["Status"]  # 'Deleting'|'Failed'|'InService'|'Pending'|'Updating'|'Update_Failed'|'Delete_Failed'
        if status == "InService":
            break
        if status in ["Failed", "Update_Failed", "Delete_Failed"]:
            raise RuntimeError(f"Space is in '{status}' state.")
        else:
            time.sleep(10)
    return res