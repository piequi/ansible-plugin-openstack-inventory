# Copyright (c) 2012, Marco Vito Moscaritolo <marco@agavee.com>
# Copyright (c) 2013, Jesse Keating <jesse.keating@rackspace.com>
# Copyright (c) 2015, Hewlett-Packard Development Company, L.P.
# Copyright (c) 2016, Rackspace Australia
# Copyright (c) 2017 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = """
    name: openstack
    plugin_type: inventory
    author:
      - Marco Vito Moscaritolo <marco@agavee.com>
      - Jesse Keating <jesse.keating@rackspace.com>
    short_description: OpenStack inventory source
    requirements:
        - openstacksdk
    extends_documentation_fragment:
        - inventory_cache
        - constructed
    description:
        - Get inventory hosts from OpenStack clouds
        - Uses openstack.(yml|yaml) YAML configuration file to configure the
            inventory plugin
        - Uses standard clouds.yaml YAML configuration file to configure cloud
            credentials
    options:
        plugin:
            description: token that ensures this is a source file for the
                'openstack' plugin.
            required: True
            choices: ['openstack']
        show_all:
            description: toggles showing all vms vs only those with a working
                IP
            type: bool
            default: 'no'
        inventory_hostname:
            description: |
                What to register as the inventory hostname.
                If set to 'uuid' the uuid of the server will be used and a
                group will be created for the server name.
                If set to 'name' the name of the server will be used unless
                there are more than one server with the same name in which
                case the 'uuid' logic will be used.
                Default is to do 'name', which is the opposite of the old
                openstack-inventory.py inventory script's option use_hostnames)
            type: string
            choices:
                - name
                - uuid
            default: "name"
        expand_hostvars:
            description: |
                Run extra commands on each host to fill in additional
                information about the host. May interrogate cinder and
                neutron and can be expensive for people with many hosts.
                (Note, the default value of this is opposite from the default
                old openstack-inventory.py inventory script's option
                expand_hostvars)
            type: bool
            default: 'no'
        private:
            description: |
                Use the private interface of each server, if it has one, as
                the host's IP in the inventory. This can be useful if you are
                running ansible inside a server in the cloud and would rather
                communicate to your servers over the private network.
            type: bool
            default: 'no'
        only_clouds:
            description: |
                List of clouds from clouds.yaml to use, instead of using
                the whole list. Beware that it doesn't apply when using data
                from inventory cache.
            type: list
            default: []
        fail_on_errors:
            description: |
                Causes the inventory to fail and return no hosts if one cloud
                has failed (for example, bad credentials or being offline).
                When set to False, the inventory will return as many hosts as
                it can from as many clouds as it can contact. (Note, the
                default value of this is opposite from the old
                openstack-inventory.py
                inventory script's option fail_on_errors)
            type: bool
            default: 'no'
        clouds_yaml_path:
            description: |
                Override path to clouds.yaml file. If this value is given it
                will be searched first. The default path for the
                ansible inventory adds /etc/ansible/openstack.yaml and
                /etc/ansible/openstack.yml to the regular locations documented
                at https://docs.openstack.org/os-client-config/latest/user/configuration.html#config-files
            type: list
            env:
                - name: OS_CLIENT_CONFIG_FILE
        debug:
            description: |
                Enable Openstack SDK debug messages. When set to True, debug
                messages are sent to stderr.
        compose:
            description: Create vars from jinja2 expressions.
            type: dictionary
            default: {}
        groups:
            description: Add hosts to group based on Jinja2 conditionals.
            type: dictionary
            default: {}
"""

EXAMPLES = """
# file must be named openstack.yaml or openstack.yml
# Make the plugin behave like the default behavior of the old script
plugin: openstack
expand_hostvars: yes
fail_on_errors: yes
"""

import collections
import sys
import logging

from ansible.errors import AnsibleParserError
from ansible.plugins.inventory import BaseInventoryPlugin, Constructable, Cacheable
from ansible.utils.display import Display

display = Display()
os_logger = logging.getLogger("openstack")

try:
    # Due to the name shadowing we should import other way
    import importlib

    sdk = importlib.import_module("openstack")
    sdk_inventory = importlib.import_module("openstack.cloud.inventory")
    client_config = importlib.import_module("openstack.config.loader")
    sdk_exceptions = importlib.import_module("openstack.exceptions")
except ImportError:
    error_msg = "Couldn't import Openstack SDK modules"
    display.error(error_msg)
    raise AnsibleParserError(error_msg)


class InventoryModule(BaseInventoryPlugin, Constructable, Cacheable):
    """ Host inventory provider for ansible using OpenStack clouds. """

    NAME = "openstack-inventory"

    def parse(self, inventory, loader, path, cache=True):

        super(InventoryModule, self).parse(inventory, loader, path)

        self._load_and_verify_plugin_config(path)

        hosts_data = None
        cache_key = self._get_cache_prefix(path)

        if cache:
            hosts_data = self._load_cache(cache_key)

        if not hosts_data:
            hosts_data = self._get_hosts_data_from_openstack()
            self._cache[cache_key] = hosts_data

        self._populate_inventory(hosts_data)

    def _load_and_verify_plugin_config(self, path):

        self._config_data = self._read_config_data(path)

        self._verify_config_data(self._config_data)

        if "clouds" in self._config_data:
            self.display.v(
                "Found clouds config file instead of plugin config. "
                "Using default configuration."
            )
            self._config_data = {}

        self.expand_hostvars = self._config_data.get("expand_hostvars", False)
        self.fail_on_errors = self._config_data.get("fail_on_errors", False)
        self.show_all = self._config_data.get("show_all", False)

        sdk.enable_logging(
            debug=self._config_data.get("debug", False), stream=sys.stderr
        )

    def _verify_config_data(self, data):

        error_msg = ""

        if not data:
            error_msg = "Config file is empty."
        elif "plugin" in data and data["plugin"] != self.NAME:
            error_msg = "Incorrect plugin config found: %s" % data["plugin"]
        elif "plugin" not in data and "clouds" not in data:
            error_msg = "Missing plugin and clouds configuration"
        elif not self._verify_config_data_types(data):
            error_msg = "Invalid config data type"

        if error_msg:
            display.error(error_msg)
            raise AnsibleParserError(error_msg)

    def _verify_config_data_types(self, data):

        clouds_yaml_path = data.get("clouds_yaml_path")
        if clouds_yaml_path and not isinstance(clouds_yaml_path, list):
            self.display.error("clouds_yaml_path must be a valid YAML list")
            return False

        debug = data.get("debug")
        if debug and not isinstance(debug, bool):
            self.display.error("debug must be a valid YAML boolean")
            return False

        expand_hostvars = data.get("expand_hostvars")
        if expand_hostvars and not isinstance(expand_hostvars, bool):
            self.display.error("expand_hostvars must be a valid YAML boolean")
            return False

        fail_on_errors = data.get("fail_on_errors")
        if fail_on_errors and not isinstance(fail_on_errors, bool):
            self.display.error("fail_on_errors must be a valid YAML boolean")
            return False

        only_clouds = data.get("only_clouds")
        if only_clouds and not isinstance(only_clouds, list):
            self.display.error("only_clouds must be a valid YAML list")
            return False

        show_all = data.get("show_all")
        if show_all and not isinstance(show_all, bool):
            self.display.error("show_all must be a valid YAML boolean")
            return False

        return True

    def _load_cache(self, cache_key):

        self.display.v("Reaading inventory data from cache: %s" % cache_key)
        cache_data = None
        try:
            cache_data = self._cache[cache_key]
        except KeyError:
            display.v("Inventory data cache not found")
        return cache_data

    def _get_hosts_data_from_openstack(self):

        self.display.v("Getting hosts from Openstack clouds")

        os_clouds_inventory = self._get_openstack_clouds_inventory(
            self._get_openstack_config_files_list()
        )

        hosts_data = []
        try:
            hosts_data = os_clouds_inventory.list_hosts(
                expand=self.expand_hostvars, fail_on_cloud_config=self.fail_on_errors
            )
        except Exception as e:
            self.display.warning("Couldn't list Openstack hosts. "
                                 "See logs for details")
            os_logger.error(e.message)
        finally:
            self.display.vv("Found %d host(s)" % len(hosts_data))
            return hosts_data

    def _get_openstack_config_files_list(self):

        clouds_yaml_path = self._config_data.get("clouds_yaml_path")
        if clouds_yaml_path:
            return clouds_yaml_path + client_config.CONFIG_FILES
        else:
            return client_config.CONFIG_FILES

    def _get_openstack_clouds_inventory(self, os_config_files_list):

        only_clouds = self._config_data.get("only_clouds", None)

        os_clouds_inventory = sdk_inventory.OpenStackInventory(
            config_files=os_config_files_list,
            private=self._config_data.get("private", False),
        )
        self.display.vv("Found %d cloud(s) in Openstack" %
                        len(os_clouds_inventory.clouds))

        selected_openstack_clouds = []
        if only_clouds:
            for cloud in os_clouds_inventory.clouds:
                self.display.vv("Looking at cloud : %s" % cloud.name)
                if cloud.name in self.only_clouds:
                    self.display.vv("Selecting cloud : %s" % cloud.name)
                    selected_openstack_clouds.append(cloud)
            os_clouds_inventory.clouds = selected_openstack_clouds

        self.display.vv("Selected %d cloud(s)" %
                        len(os_clouds_inventory.clouds))

        return os_clouds_inventory

    def _populate_inventory(self, hosts_data):

        self.servers = collections.defaultdict(list)
        self.groups = collections.defaultdict(list)
        self.hostvars = {}

        self._populate_inventory_hosts(hosts_data)

        self._populate_inventory_variables()
        self._populate_inventory_groups()

    def _populate_inventory_hosts(self, hosts_data):

        use_server_id = self._config_data.get("inventory_hostname", "name") != "name"

        # remove unreachable servers if show_all is False
        for server in hosts_data:
            if "interface_ip" not in server and not self.show_all:
                continue
            self.servers[server["name"]].append(server)

        # add remaining servers data to inventory
        for name, server_data in self.servers.items():
            if len(server_data) == 1 and not use_server_id:
                self._store_host_data(name, server_data[0])
            else:
                server_ids = set()
                # Trap for duplicate results
                for server in server_data:
                    server_ids.add(server["id"])
                if len(server_ids) == 1 and not use_server_id:
                    self._store_host_data(name, server_data[0])
                else:
                    for data in server_data:
                        self._store_host_data(data["id"], data, namegroup=True)

    def _store_host_data(self, host, server_data, namegroup=False):

        self.hostvars[host] = dict(
            ansible_ssh_host=server_data["interface_ip"],
            ansible_host=server_data["interface_ip"],
            openstack=server_data,
        )

        self.inventory.add_host(host)

        for group in self._get_group_names_from_server_data(
            server_data, namegroup=namegroup
        ):
            self.groups[group].append(host)

    def _get_group_names_from_server_data(self, server_data, namegroup=True):

        server_groups = []

        region = server_data["region"]
        cloud = server_data["cloud"]
        metadata = server_data.get("metadata", {})

        # Create a group on cloud
        server_groups.append(cloud)

        # Create group on region
        if region:
            server_groups.append(region)

        # Create group on cloud_region
        server_groups.append("%s_%s" % (cloud, region))

        # Create group on metadata group key
        if "group" in metadata:
            server_groups.append(metadata["group"])

        # Create group on every metadata group
        for extra_group in metadata.get("groups", "").split(","):
            if extra_group:
                server_groups.append(extra_group.strip())

        # Create group on instance id
        server_groups.append("instance-%s" % server_data["id"])

        # Create group on instance name
        if namegroup:
            server_groups.append(server_data["name"])

        # Create group on flavor and image names
        for key in ("flavor", "image"):
            if "name" in server_data[key]:
                server_groups.append("%s-%s" % (key, server_data[key]["name"]))

        # Create group on every metadata
        for key, value in iter(metadata.items()):
            server_groups.append("meta-%s_%s" % (key, value))

        az = server_data.get("az", None)
        if az:
            # Make groups for az, region_az and cloud_region_az
            server_groups.append(az)
            server_groups.append("%s_%s" % (region, az))
            server_groups.append("%s_%s_%s" % (cloud, region, az))

        return server_groups

    def _populate_inventory_variables(self):

        for host in self.hostvars:
            self._set_composite_vars(
                self._config_data.get("compose"), self.hostvars[host], host
            )

        for variable in self.hostvars[host]:
            self.inventory.set_variable(host, variable, self.hostvars[host][variable])

    def _populate_inventory_groups(self):

        for host in self.hostvars:
            self._add_host_to_composed_groups(
                self._config_data.get("groups"), self.hostvars[host], host
            )
            self._add_host_to_keyed_groups(
                self._config_data.get("keyed_groups"), self.hostvars[host], host
            )

        for group_name, hosts in self.groups.items():
            group = self.inventory.add_group(group_name)
            for host in hosts:
                self.inventory.add_child(group, host)

    def verify_file(self, path):
        if super(InventoryModule, self).verify_file(path):
            for fn in ("openstack", "clouds"):
                for suffix in ("yaml", "yml"):
                    maybe = "{fn}.{suffix}".format(fn=fn, suffix=suffix)
                    if path.endswith(maybe):
                        self.display.v("Valid plugin config file found")
                        return True
        return False
