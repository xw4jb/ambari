#!/usr/bin/env python3
"""
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import urllib.request, urllib.error, urllib.parse
import ambari_simplejson as json  # simplejson is much faster comparing to Python 2.6 json module and has the same functions set.
import logging
import traceback

from ambari_commons.urllib_handlers import RefreshHeaderProcessor
from resource_management.libraries.functions.curl_krb_request import curl_krb_request
from resource_management.libraries.functions.curl_krb_request import (
  DEFAULT_KERBEROS_KINIT_TIMER_MS,
)
from resource_management.libraries.functions.curl_krb_request import (
  KERBEROS_KINIT_TIMER_PARAMETER,
)
from resource_management.core.environment import Environment

ERROR_LABEL = "{0} NodeManager{1} {2} unhealthy."
OK_LABEL = "All NodeManagers are healthy"

NODEMANAGER_HTTP_ADDRESS_KEY = "{{yarn-site/yarn.resourcemanager.webapp.address}}"
NODEMANAGER_HTTPS_ADDRESS_KEY = (
  "{{yarn-site/yarn.resourcemanager.webapp.https.address}}"
)
YARN_HTTP_POLICY_KEY = "{{yarn-site/yarn.http.policy}}"

KERBEROS_KEYTAB = "{{yarn-site/yarn.nodemanager.webapp.spnego-keytab-file}}"
KERBEROS_PRINCIPAL = "{{yarn-site/yarn.nodemanager.webapp.spnego-principal}}"
SECURITY_ENABLED_KEY = "{{cluster-env/security_enabled}}"
SMOKEUSER_KEY = "{{cluster-env/smokeuser}}"
EXECUTABLE_SEARCH_PATHS = "{{kerberos-env/executable_search_paths}}"

CONNECTION_TIMEOUT_KEY = "connection.timeout"
CONNECTION_TIMEOUT_DEFAULT = 5.0

LOGGER_EXCEPTION_MESSAGE = "[Alert] NodeManager Health Summary on {0} fails:"
logger = logging.getLogger("ambari_alerts")

QRY = "Hadoop:service=ResourceManager,name=RMNMInfo"


def get_tokens():
  """
  Returns a tuple of tokens in the format {{site/property}} that will be used
  to build the dictionary passed into execute
  """
  return (
    NODEMANAGER_HTTP_ADDRESS_KEY,
    NODEMANAGER_HTTPS_ADDRESS_KEY,
    EXECUTABLE_SEARCH_PATHS,
    YARN_HTTP_POLICY_KEY,
    SMOKEUSER_KEY,
    KERBEROS_KEYTAB,
    KERBEROS_PRINCIPAL,
    SECURITY_ENABLED_KEY,
  )


def execute(configurations={}, parameters={}, host_name=None):
  """
  Returns a tuple containing the result code and a pre-formatted result label

  Keyword arguments:
  configurations (dictionary): a mapping of configuration key to value
  parameters (dictionary): a mapping of script parameter key to value
  host_name (string): the name of this host where the alert is running
  """

  if configurations is None:
    return ("UNKNOWN", ["There were no configurations supplied to the script."])

  scheme = "http"
  http_uri = None
  https_uri = None
  http_policy = "HTTP_ONLY"

  security_enabled = False
  if SECURITY_ENABLED_KEY in configurations:
    security_enabled = str(configurations[SECURITY_ENABLED_KEY]).upper() == "TRUE"

  executable_paths = None
  if EXECUTABLE_SEARCH_PATHS in configurations:
    executable_paths = configurations[EXECUTABLE_SEARCH_PATHS]

  kerberos_keytab = None
  if KERBEROS_KEYTAB in configurations:
    kerberos_keytab = configurations[KERBEROS_KEYTAB]

  kerberos_principal = None
  if KERBEROS_PRINCIPAL in configurations:
    kerberos_principal = configurations[KERBEROS_PRINCIPAL]
    kerberos_principal = kerberos_principal.replace("_HOST", host_name)

  if NODEMANAGER_HTTP_ADDRESS_KEY in configurations:
    http_uri = configurations[NODEMANAGER_HTTP_ADDRESS_KEY]

  if NODEMANAGER_HTTPS_ADDRESS_KEY in configurations:
    https_uri = configurations[NODEMANAGER_HTTPS_ADDRESS_KEY]

  if YARN_HTTP_POLICY_KEY in configurations:
    http_policy = configurations[YARN_HTTP_POLICY_KEY]

  if SMOKEUSER_KEY in configurations:
    smokeuser = configurations[SMOKEUSER_KEY]

  # parse script arguments
  connection_timeout = CONNECTION_TIMEOUT_DEFAULT
  if CONNECTION_TIMEOUT_KEY in parameters:
    connection_timeout = float(parameters[CONNECTION_TIMEOUT_KEY])

  kinit_timer_ms = parameters.get(
    KERBEROS_KINIT_TIMER_PARAMETER, DEFAULT_KERBEROS_KINIT_TIMER_MS
  )

  # determine the right URI and whether to use SSL
  uri = http_uri
  if http_policy == "HTTPS_ONLY":
    scheme = "https"

    if https_uri is not None:
      uri = https_uri

  uri = str(host_name) + ":" + uri.split(":")[1]
  live_nodemanagers_qry = f"{scheme}://{uri}/jmx?qry={QRY}"
  convert_to_json_failed = False
  response_code = None
  try:
    if (
      kerberos_principal is not None
      and kerberos_keytab is not None
      and security_enabled
    ):
      env = Environment.get_instance()

      # curl requires an integer timeout
      curl_connection_timeout = int(connection_timeout)

      url_response, error_msg, time_millis = curl_krb_request(
        env.tmp_dir,
        kerberos_keytab,
        kerberos_principal,
        live_nodemanagers_qry,
        "nm_health_summary_alert",
        executable_paths,
        False,
        "NodeManager Health Summary",
        smokeuser,
        connection_timeout=curl_connection_timeout,
        kinit_timer_ms=kinit_timer_ms,
      )

      try:
        url_response_json = json.loads(url_response)
        live_nodemanagers = json.loads(
          find_value_in_jmx(
            url_response_json, "LiveNodeManagers", live_nodemanagers_qry
          )
        )
      except ValueError as error:
        convert_to_json_failed = True
        logger.exception(
          "[Alert][{0}] Convert response to json failed or json doesn't contain needed data: {1}".format(
            "NodeManager Health Summary", str(error)
          )
        )

      if convert_to_json_failed:
        response_code, error_msg, time_millis = curl_krb_request(
          env.tmp_dir,
          kerberos_keytab,
          kerberos_principal,
          live_nodemanagers_qry,
          "nm_health_summary_alert",
          executable_paths,
          True,
          "NodeManager Health Summary",
          smokeuser,
          connection_timeout=curl_connection_timeout,
          kinit_timer_ms=kinit_timer_ms,
        )
    else:
      live_nodemanagers = json.loads(
        get_value_from_jmx(
          live_nodemanagers_qry, "LiveNodeManagers", connection_timeout
        )
      )

    if (
      kerberos_principal is not None
      and kerberos_keytab is not None
      and security_enabled
    ):
      if response_code in [200, 307] and convert_to_json_failed:
        return (
          "UNKNOWN",
          [f"HTTP {str(response_code)} response (metrics unavailable)"],
        )
      elif convert_to_json_failed and response_code not in [200, 307]:
        raise Exception(
          "[Alert][NodeManager Health Summary] Getting data from {0} failed with http code {1}".format(
            str(live_nodemanagers_qry), str(response_code)
          )
        )

    unhealthy_count = 0

    for nodemanager in live_nodemanagers:
      health_report = nodemanager["State"]
      if health_report == "UNHEALTHY":
        unhealthy_count += 1

    if unhealthy_count == 0:
      result_code = "OK"
      label = OK_LABEL
    else:
      result_code = "CRITICAL"
      if unhealthy_count == 1:
        label = ERROR_LABEL.format(unhealthy_count, "", "is")
      else:
        label = ERROR_LABEL.format(unhealthy_count, "s", "are")

  except:
    label = traceback.format_exc()
    result_code = "UNKNOWN"

  return (result_code, [label])


def get_value_from_jmx(query, jmx_property, connection_timeout):
  response = None

  try:
    # use a customer header process that will look for the non-standard
    # "Refresh" header and attempt to follow the redirect
    url_opener = urllib.request.build_opener(RefreshHeaderProcessor())
    response = url_opener.open(query, timeout=connection_timeout)

    data = response.read()
    data_dict = json.loads(data)
    return find_value_in_jmx(data_dict, jmx_property, query)
  finally:
    if response is not None:
      try:
        response.close()
      except:
        pass


def find_value_in_jmx(data_dict, jmx_property, query):
  json_data = data_dict["beans"][0]

  if jmx_property not in json_data:
    beans = data_dict["beans"]
    for jmx_prop_list_item in beans:
      if "name" in jmx_prop_list_item and jmx_prop_list_item["name"] == QRY:
        if jmx_property not in jmx_prop_list_item:
          raise Exception(f"Unable to find {jmx_property} in JSON from {query} ")
        json_data = jmx_prop_list_item

  return json_data[jmx_property]
