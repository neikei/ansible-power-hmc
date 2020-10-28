#!/usr/bin/python

# Copyright: (c) 2018- IBM, Inc
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type

ANSIBLE_METADATA = {
    'metadata_version': '1.1',
    'status': ['preview'],
    'supported_by': 'community'
}

DOCUMENTATION = '''
---
module: hmc_powervm

short_description: Create/Delete an AIX or Linux partition

description:
    - "Updates the HMC by installing a corrective service package located on an FTP/SFTP/NFS server or HMC hard disk"
    - "Or Upgrades the HMC by obtaining  the required  files  from a remote server or from the HMC hard disk. The files are transferred
       onto a special partition on the HMC hard disk. After the files have been transferred, HMC will boot from this partition
       and perform the upgrade"

version_added: "2.9"

options:
    hmc_host:
        description:
            - The ipaddress or hostname of HMC
        required: true
        type: str
    hmc_auth:
        description:
            - Username and Password credential of HMC
        required: true
        type: dict
        suboptions:
            userid:
                description:
                    - HMC user name
                required: true
                type: str
            password:
                description:
                    - HMC password
                type: str
    system_name:
        description:
            - The name of the managed system
        required: true
        type: str
    vm_name:
        description:
            - The name of the powervm partition to create/delete
        required: true
        type: str
    proc:
        description:
            - The number of dedicated processors to create partition
        required: true
        type: str
    mem:
        description:
            - The value of dedicated memory value in megabytes to create partition
        required: true
        type: str
    os_type:
        description:
            - "Type of logical partition to be created"
            - "aix_linux: for AIX or Linux type of OS"
            - "ibmi: for IBM i operating system"
        required: true
        type: str
        choices: ['aix_linux', 'ibmi']
    state:
        description:
            - "The desired build state of the target hmc"
            - "facts: Does not change anything on the HMC and returns current driver/build level of HMC"
            - "update: Ensures the target HMC is updated with given corrective service ISO image"
            - "upgrade: Ensures the target HMC is upgraded with given upgrade files"
        required: true
        type: str
        choices: ['present', 'absent']

author:
    - Anil Vijayan (@yourhandle)
'''

EXAMPLES = '''
# Pass in a message
- name: Test with a message
  my_new_test_module:
    name: hello world

# pass in a message and have changed true
- name: Test with a message and changed output
  my_new_test_module:
    name: hello world
    new: true

# fail the module
- name: Test failure of the module
  my_new_test_module:
    name: fail me
'''

RETURN = '''
original_message:
    description: The original name param that was passed in
    type: str
    returned: always
message:
    description: The output message that the sample module generates
    type: str
    returned: always
'''

import sys
from ansible.module_utils.basic import AnsibleModule
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_cli_client import HmcCliConnection
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_resource import Hmc
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_exceptions import HmcError
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_exceptions import ParameterError
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_exceptions import Error
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_rest_client import logon
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_rest_client import getPartitionTemplate
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_rest_client import getManagedSystem
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_rest_client import copyPartitionTemplate
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_rest_client import updatePartitionTemplate
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_rest_client import deployPartitionTemplate
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_rest_client import transformPartitionTemplate
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_rest_client import checkPartitionTemplate
from ansible_collections.ibm.power_hmc.plugins.module_utils.hmc_rest_client import deletePartitionTemplate

# Generic setting for log initializing and log rotation
import logging
LOG_FILENAME = "/tmp/hmc_ansible.log"
logger = logging.getLogger(__name__)

HMC_REBOOT_TIMEOUT = 60


def init_logger():
    logging.basicConfig(
        filename=LOG_FILENAME,
        format='[%(asctime)s] %(levelname)s: [%(funcName)s] %(message)s',
        level=logging.DEBUG)


def validate_proc_mem(system_doc, proc, mem):

    curr_avail_proc_units = system_doc.xpath('//CurrentAvailableSystemProcessorUnits')[0].text
    int_avail_proc = int(float(curr_avail_proc_units))

    curr_avail_mem = system_doc.xpath('//CurrentAvailableSystemMemory')[0].text
    int_avail_mem = int(curr_avail_mem)
    curr_avail_lmb = system_doc.xpath('//CurrentLogicalMemoryBlockSize')[0].text
    lmb = int(curr_avail_lmb)

    if proc > int_avail_proc:
        raise HmcError("Available system proc units is not enough. Provide value on or below {0}".format(str(int_avail_proc)))

    if mem % lmb > 0:
        raise HmcError("Requested mem value not in mutiple of block size:{0}".format(curr_avail_lmb))

    if mem > int_avail_mem:
        raise HmcError("Available system memory is not enough. Provide value on or below {0}".format(curr_avail_mem))


def addTaggedIOdetails(lpar_template_doc):
    taggedIO_payload = '''<iBMiPartitionTaggedIO kxe="false" kb="CUD" schemaVersion="V1_0">
                <Metadata>
                    <Atom/>
                </Metadata>
                <console kxe="false" kb="CUD">HMC</console>
                <operationsConsole kxe="false" kb="CUD">NONE</operationsConsole>
                <loadSource kb="CUD" kxe="false">NONE</loadSource>
                <alternateLoadSource kxe="false" kb="CUD">NONE</alternateLoadSource>
                <alternateConsole kxe="false" kb="CUD">NONE</alternateConsole>
            </iBMiPartitionTaggedIO>'''

    ioConfigurationTag = lpar_template_doc.xpath("//ioConfiguration/isUseCapturedPhysicalIOInformationEnabled")[0]
    ioConfigurationTag.addnext(etree.XML(taggedIO_payload))


def create_partition(module, params):
    changed = False
    hmc_conn = None
    session = None
    system_uuid = None
    server_doc = None
    hmc_host = params['hmc_host']
    hmc_user = params['hmc_auth']['userid']
    password = params['hmc_auth']['password']
    system_name = params['system_name']
    vm_name = params['vm_name']
    proc = params['proc']
    mem = params['mem']
    os_type = params['os_type']

    hmc_conn = HmcCliConnection(module, hmc_host, hmc_user, password)
    hmc = Hmc(hmc_conn)

    try:
        session = logon(hmc_host, hmc_user, password)
    except Exception as error:
        logger.debug(repr(error))
        module.fail_json(msg="Logon to HMC failed")

    try:
        system_uuid, server_doc = getManagedSystem(hmc_host, session, system_name)
    except Exception as error:
        logger.debug(repr(error))
        module.fail_json(msg="Fetch of managed system info failed")
    if not system_uuid:
        module.fail_json(msg="Given system is not present")

    validate_proc_mem(server_doc, int(proc), int(mem))

    try:
        if os_type == 'aix_linux':
            reference_template = "QuickStart_lpar_rpa_3"
        else:
            reference_template = "QuickStart_lpar_IBMi_3"
        copyPartitionTemplate(hmc_host, session, reference_template, "draft_ansible_powervm_create")
        max_lpars = server_doc.xpath("//MaximumPartitions")[0].text
        next_lpar_id = hmc.getNextPartitionID(system_name, max_lpars)
        logger.debug("Next Partiion ID: %s", str(next_lpar_id))
        logger.debug("CEC uuid: %s", system_uuid)

        resp = checkPartitionTemplate(hmc_host, session, "draft_ansible_powervm_create", system_uuid)
        draft_uuid = resp.xpath("//ParameterName[text()='TEMPLATE_UUID']/following-sibling::ParameterValue")[0].text
        logger.debug(draft_uuid)
        draft_template_xml = getPartitionTemplate(hmc_host, session, uuid=draft_uuid)

        config_dict = {'lpar_id': str(next_lpar_id)}
        config_dict['vm_name'] = vm_name
        config_dict['proc'] = proc
        config_dict['mem'] = mem
        if os_type == 'ibmi':
            addTaggedIOdetails(draft_template_xml)
        updatePartitionTemplate(hmc_host, session, draft_uuid, draft_template_xml, config_dict)
        transformPartitionTemplate(hmc_host, session, draft_uuid, system_uuid)
        deployPartitionTemplate(hmc_host, session, draft_uuid, system_uuid)
    except Exception as error:
        logger.debug("Line number: %d exception: %s", sys.exc_info()[2].tb_lineno, repr(error))
        module.fail_json(msg=repr(error))
    finally:
        try:
            deletePartitionTemplate(hmc_host, session, "draft_ansible_powervm_create")
        except Exception as del_error:
            module.fail_json(msg=repr(del_error))

    return changed, None


def perform_task(module):

    params = module.params
    actions = {
        "present": create_partition,
    }
    try:
        return actions[params['state']](module, params)
    except (ParameterError, HmcError, Error) as error:
        return False, repr(error)


def run_module():

    # define available arguments/parameters a user can pass to the module
    module_args = dict(
        hmc_host=dict(type='str', required=True),
        hmc_auth=dict(type='dict',
                      required=True,
                      no_log=True,
                      options=dict(
                          userid=dict(required=True, type='str'),
                          password=dict(type='str'),
                      )
                      ),
        system_name=dict(type='str', required=True),
        vm_name=dict(type='str', required=True),
        proc=dict(type='str', required=True),
        mem=dict(type='str', required=True),
        os_type=dict(type='str', required=True, choices=['aix_linux', 'ibmi']),
        state=dict(required=True, type='str',
                   choices=['present', 'absent'])
    )

    module = AnsibleModule(
        argument_spec=module_args,
    )

    if module._verbosity >= 1:
        init_logger()

    changed, result = perform_task(module)

    if isinstance(result, str):
        module.fail_json(msg=result)

    module.exit_json(changed=changed, build_info=result)


def main():
    run_module()


if __name__ == '__main__':
    main()
