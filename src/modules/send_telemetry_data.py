# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Module to send telemetry data to Kusto Cluster/Log Analytics Workspace and create an HTML report.
"""

import logging
import os
from datetime import datetime
from typing import Dict, Any
import base64
import hashlib
import hmac
import json
import requests
from azure.kusto.data import KustoConnectionStringBuilder
from azure.kusto.data.data_format import DataFormat
from azure.kusto.ingest import QueuedIngestClient, IngestionProperties, ReportLevel
from ansible.module_utils.basic import AnsibleModule

try:
    from ansible.module_utils.sap_automation_qa import (
        SapAutomationQA,
        TestStatus,
        TelemetryDataDestination,
    )
except ImportError:
    from src.module_utils.sap_automation_qa import (
        SapAutomationQA,
        TestStatus,
        TelemetryDataDestination,
    )

DOCUMENTATION = r"""
---
module: send_telemetry_data
short_description: Sends SAP automation test results as telemetry data
description:
    - This module sends test result data to Azure telemetry systems
    - Supports sending to Azure Data Explorer (Kusto) or Log Analytics Workspace
    - Also writes a local log file for backup and debugging
    - Handles authentication and formatting for different Azure services
options:
    test_group_json_data:
        description:
            - Dictionary containing the telemetry data to be sent
            - Should include TestGroupInvocationId and other test metadata
        type: dict
        required: true
    telemetry_data_destination:
        description:
            - Where to send the telemetry data
            - Use "azuredataexplorer" for Azure Data Explorer
            - Use "azureloganalytics" for Log Analytics Workspace
        type: str
        required: true
    laws_workspace_id:
        description:
            - Log Analytics workspace ID
            - Required when telemetry_data_destination is "azureloganalytics"
        type: str
        required: false
    laws_shared_key:
        description:
            - Log Analytics workspace shared key
            - Required when telemetry_data_destination is "azureloganalytics"
        type: str
        required: false
    telemetry_table_name:
        description:
            - Table name for storing the telemetry data
            - In Log Analytics, this becomes the Log-Type header
        type: str
        required: false
    adx_database_name:
        description:
            - Azure Data Explorer database name
            - Required when telemetry_data_destination is "azuredataexplorer"
        type: str
        required: false
    adx_cluster_fqdn:
        description:
            - Azure Data Explorer cluster FQDN
            - Required when telemetry_data_destination is "azuredataexplorer"
        type: str
        required: false
    adx_client_id:
        description:
            - Client ID for Azure Data Explorer authentication
            - Required when telemetry_data_destination is "azuredataexplorer"
        type: str
        required: false
    workspace_directory:
        description:
            - Directory for storing local log files
            - Logs will be created in {workspace_directory}/logs/
        type: str
        required: true
author:
    - Microsoft Corporation
notes:
    - Uses managed identity authentication for Azure Data Explorer
    - Uses shared key authentication for Log Analytics Workspace
    - Always writes a local log file regardless of telemetry destination
requirements:
    - python >= 3.6
    - azure-kusto-data
    - azure-kusto-ingest
    - requests
    - pandas
"""

EXAMPLES = r"""
- name: Send telemetry data to Azure Log Analytics
  send_telemetry_data:
    test_group_json_data: "{{ test_results }}"
    telemetry_data_destination: "azureloganalytics"
    laws_workspace_id: "{{ laws_workspace_id }}"
    laws_shared_key: "{{ laws_shared_key }}"
    telemetry_table_name: "SAPAutomationQAResults"
    workspace_directory: "/var/log/sap-automation-qa"
  register: telemetry_result

- name: Send telemetry data to Azure Data Explorer (Kusto)
  send_telemetry_data:
    test_group_json_data: "{{ test_results }}"
    telemetry_data_destination: "azuredataexplorer"
    adx_database_name: "SAPAutomationQA"
    adx_cluster_fqdn: "https://cluster.region.kusto.windows.net"
    adx_client_id: "{{ adx_client_id }}"
    telemetry_table_name: "TestResults"
    workspace_directory: "/var/log/sap-automation-qa"
  register: telemetry_result

- name: Only log data locally without sending to Azure
  send_telemetry_data:
    test_group_json_data: "{{ test_results }}"
    telemetry_data_destination: "local"
    workspace_directory: "/var/log/sap-automation-qa"
  register: telemetry_result
"""

RETURN = r"""
status:
    description: Status of the telemetry sending operation
    returned: always
    type: str
    sample: "SUCCESS"
message:
    description: Detailed message about the operation
    returned: always
    type: str
    sample: "Telemetry data sent to azureloganalytics. Telemetry data written to /var/log/sap-automation-qa/logs/123456.log."
telemetry_data:
    description: Copy of the data that was sent
    returned: always
    type: dict
    sample: {"TestGroupInvocationId": "123456", "TestResult": "PASS"}
telemetry_data_destination:
    description: Where the data was sent
    returned: always
    type: str
    sample: "azureloganalytics"
start:
    description: Start time of the operation
    returned: always
    type: str
    sample: "2023-01-01T12:00:00"
end:
    description: End time of the operation
    returned: always
    type: str
    sample: "2023-01-01T12:00:05"
data_sent:
    description: Whether the data was successfully sent to the destination
    returned: always
    type: bool
    sample: true
"""

LAWS_RESOURCE = "/api/logs"
LAWS_METHOD = "POST"
LAWS_CONTENT_TYPE = "application/json"


class TelemetryDataSender(SapAutomationQA):
    """
    Class to send telemetry data to Kusto Cluster/Log Analytics Workspace and create an HTML report.
    """

    def __init__(self, module_params: Dict[str, Any]):
        super().__init__()
        self.module_params = module_params
        self.result.update(
            {
                "telemetry_data": module_params["test_group_json_data"],
                "telemetry_data_destination": module_params["telemetry_data_destination"],
                "start": datetime.now(),
                "end": datetime.now(),
                "data_sent": False,
                "data_logged": False,
            }
        )

    def _get_authorization_for_log_analytics(
        self,
        workspace_id: str,
        workspace_shared_key: str,
        content_length: int,
        date: str,
    ) -> str:
        """
        Builds the authorization header for Azure Log Analytics.

        :param workspace_id: Workspace ID for Azure Log Analytics.
        :type workspace_id: str
        :param workspace_shared_key: Workspace Key for Azure Log Analytics.
        :type workspace_shared_key: str
        :param content_length: Length of the payload.
        :type content_length: int
        :param date: Date and time of the request.
        :type date: str
        :return: Authorization header.
        :rtype: str
        """
        string_to_hash = (
            f"{LAWS_METHOD}\n{content_length}\n{LAWS_CONTENT_TYPE}\nx-ms-date:"
            + f"{date}\n{LAWS_RESOURCE}"
        )
        encoded_hash = base64.b64encode(
            hmac.new(
                base64.b64decode(workspace_shared_key),
                bytes(string_to_hash, "UTF-8"),
                digestmod=hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return f"SharedKey {workspace_id}:{encoded_hash}"

    def send_telemetry_data_to_azuredataexplorer(self, telemetry_json_data: str) -> Any:
        """
        Sends telemetry data to Azure Data Explorer.

        :param telemetry_json_data: The JSON data to be sent to Azure Data Explorer.
        :type telemetry_json_data: str
        :return: The response from the Kusto API.
        :rtype: Any
        """
        import pandas as pd

        telemetry_json_data = json.loads(telemetry_json_data)
        data_frame = pd.DataFrame(
            [telemetry_json_data.values()], columns=telemetry_json_data.keys()
        )
        ingestion_properties = IngestionProperties(
            database=self.module_params["adx_database_name"],
            table=self.module_params["telemetry_table_name"],
            data_format=DataFormat.JSON,
            report_level=ReportLevel.FailuresAndSuccesses,
        )
        kcsb = KustoConnectionStringBuilder.with_aad_managed_service_identity_authentication(
            connection_string=self.module_params["adx_cluster_fqdn"],
            client_id=self.module_params["adx_client_id"],
        )
        client = QueuedIngestClient(kcsb)
        response = client.ingest_from_dataframe(data_frame, ingestion_properties)
        self.log(
            logging.INFO,
            f"Response from Kusto: {response}",
        )
        return response

    def send_telemetry_data_to_azureloganalytics(
        self, telemetry_json_data: str
    ) -> requests.Response:
        """
        Sends telemetry data to Azure Log Analytics Workspace.

        :param telemetry_json_data: JSON data to be sent to Log Analytics.
        :type telemetry_json_data: str
        :return: Response from the Log Analytics API.
        :rtype: requests.Response
        """
        utc_datetime = datetime.now().strftime("%a, %d %b %Y %H:%M:%S GMT")
        authorization_header = self._get_authorization_for_log_analytics(
            workspace_id=self.module_params["laws_workspace_id"],
            workspace_shared_key=self.module_params["laws_shared_key"],
            content_length=len(telemetry_json_data),
            date=utc_datetime,
        )

        response = requests.post(
            url=f"https://{self.module_params['laws_workspace_id']}.ods.opinsights.azure.com"
            + f"{LAWS_RESOURCE}?api-version=2016-04-01",
            data=telemetry_json_data,
            headers={
                "content-type": LAWS_CONTENT_TYPE,
                "Authorization": authorization_header,
                "Log-Type": self.module_params["telemetry_table_name"],
                "x-ms-date": utc_datetime,
            },
            timeout=30,
        )
        self.log(
            logging.INFO,
            f"Response from Log Analytics: {response}",
        )
        return response

    def validate_params(self) -> bool:
        """
        Validate the telemetry data destination parameters.

        :return: True if the parameters are valid, False otherwise.
        :rtype: bool
        """
        telemetry_data_destination = self.module_params.get("telemetry_data_destination")

        if telemetry_data_destination == TelemetryDataDestination.LOG_ANALYTICS.value:
            if (
                "laws_workspace_id" not in self.module_params
                or "laws_shared_key" not in self.module_params
                or "telemetry_table_name" not in self.module_params
            ):
                return False
        elif telemetry_data_destination == TelemetryDataDestination.KUSTO.value:
            required_params = [
                "adx_database_name",
                "telemetry_table_name",
                "adx_cluster_fqdn",
                "adx_client_id",
            ]
            missing_params = [param for param in required_params if param not in self.module_params]
            if missing_params:
                return False
        return True

    def write_log_file(self) -> None:
        """
        Writes the telemetry data to a log file.
        """
        try:
            log_folder = os.path.join(self.module_params["workspace_directory"], "logs")
            os.makedirs(log_folder, exist_ok=True)
            log_file_path = os.path.join(
                log_folder,
                f"{self.result['telemetry_data']['TestGroupInvocationId']}.log",
            )
            with open(log_file_path, "a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(self.result["telemetry_data"]))
                log_file.write("\n")

            self.result["message"] += f"Telemetry data written to {log_file_path}. "
            self.result.update(
                {
                    "status": TestStatus.SUCCESS.value,
                    "data_sent": True,
                }
            )
        except Exception as ex:
            self.handle_error(ex)

    def send_telemetry_data(self) -> None:
        """
        Sends telemetry data to the specified destination.
        """
        if self.module_params["telemetry_data_destination"] in [
            TelemetryDataDestination.KUSTO.value,
            TelemetryDataDestination.LOG_ANALYTICS.value,
        ]:
            self.log(logging.INFO, "Validating parameters for telemetry data destination ")

            if not self.validate_params():
                self.result["status"] = (
                    "Invalid parameters for telemetry data destination. Data will not be sent."
                )
                return

            self.log(
                logging.INFO,
                f"Sending telemetry data to {self.module_params['telemetry_data_destination']}",
            )

            try:
                method_name = (
                    "send_telemetry_data_to_"
                    + f"{self.module_params['telemetry_data_destination']}"
                )
                getattr(self, method_name)(json.dumps(self.result["telemetry_data"]))
                self.result[
                    "message"
                ] += f"Telemetry data sent to {self.module_params['telemetry_data_destination']}. "
                self.result.update(
                    {
                        "status": TestStatus.SUCCESS.value,
                        "data_sent": True,
                    }
                )
            except Exception as ex:
                self.handle_error(ex)
        else:
            self.log(
                logging.ERROR,
                "Invalid telemetry data destination specified "
                + f"{self.module_params['telemetry_data_destination']}",
            )

    def get_result(self) -> Dict[str, Any]:
        """
        Returns the result dictionary.

        :return: The result dictionary containing the status of the operation.
        :rtype: Dict[str, Any]
        """
        self.result["end"] = datetime.now()
        return self.result


def run_module() -> None:
    """
    Sets up and runs the telemetry data sending module with the specified arguments.
    """
    module_args = dict(
        test_group_json_data=dict(type="dict", required=True),
        telemetry_data_destination=dict(type="str", required=True),
        laws_workspace_id=dict(type="str", required=False),
        laws_shared_key=dict(type="str", required=False),
        telemetry_table_name=dict(type="str", required=False),
        adx_database_name=dict(type="str", required=False),
        adx_cluster_fqdn=dict(type="str", required=False),
        adx_client_id=dict(type="str", required=False),
        workspace_directory=dict(type="str", required=True),
    )

    module = AnsibleModule(argument_spec=module_args, supports_check_mode=True)
    sender = TelemetryDataSender(module.params)

    sender.write_log_file()
    sender.send_telemetry_data()

    module.exit_json(**sender.get_result())


def main() -> None:
    """
    Entry point of the script.
    """
    run_module()


if __name__ == "__main__":
    main()
