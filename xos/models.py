from django.db import models
from core.models import Service, PlCoreBase, Slice, Instance, Tenant, TenantWithContainer, Node, Image, User, Flavor, Subscriber, NetworkParameter, NetworkParameterType, Port, AddressPool
from core.models.plcorebase import StrippedCharField
import os
from django.db import models, transaction
from django.forms.models import model_to_dict
from django.db.models import Q
from operator import itemgetter, attrgetter, methodcaller
from core.models import Tag
from core.models.service import LeastLoadedNodeScheduler
import traceback
from xos.exceptions import *
from xos.config import Config


class ConfigurationError(Exception):
    pass


VROUTER_KIND = "vROUTER"
APP_LABEL = "vrouter"

# NOTE: don't change VROUTER_KIND unless you also change the reference to it
#   in tosca/resources/network.py

CORD_USE_VTN = getattr(Config(), "networking_use_vtn", False)


class VRouterService(Service):
    KIND = VROUTER_KIND

    class Meta:
        app_label = APP_LABEL
        verbose_name = "vRouter Service"
        proxy = True

    default_attributes = {
        "rest_hostname": "",
        "rest_port": "8181",
        "rest_user": "onos",
        "rest_pass": "rocks"
    }

    @property
    def rest_hostname(self):
        return self.get_attribute("rest_hostname", self.default_attributes["rest_hostname"])

    @rest_hostname.setter
    def rest_hostname(self, value):
        self.set_attribute("rest_hostname", value)

    @property
    def rest_port(self):
        return self.get_attribute("rest_port", self.default_attributes["rest_port"])

    @rest_port.setter
    def rest_port(self, value):
        self.set_attribute("rest_port", value)

    @property
    def rest_user(self):
        return self.get_attribute("rest_user", self.default_attributes["rest_user"])

    @rest_user.setter
    def rest_user(self, value):
        self.set_attribute("rest_user", value)

    @property
    def rest_pass(self):
        return self.get_attribute("rest_pass", self.default_attributes["rest_pass"])

    @rest_pass.setter
    def rest_pass(self, value):
        self.set_attribute("rest_pass", value)

    def ip_to_mac(self, ip):
        (a, b, c, d) = ip.split('.')
        return "02:42:%02x:%02x:%02x:%02x" % (int(a), int(b), int(c), int(d))

    def get_gateways(self):
        gateways = []

        aps = self.addresspools.all()
        for ap in aps:
            gateways.append({"gateway_ip": ap.gateway_ip, "gateway_mac": ap.gateway_mac})

        return gateways

    def get_address_pool(self, name):
        ap = AddressPool.objects.filter(name=name, service=self)
        if not ap:
            raise Exception("vRouter unable to find addresspool %s" % name)
        return ap[0]

    def get_tenant(self, **kwargs):
        address_pool_name = kwargs.pop("address_pool_name")

        ap = self.get_address_pool(address_pool_name)

        ip = ap.get_address()
        if not ip:
            raise Exception("AddressPool '%s' has run out of addresses." % ap.name)

        t = VRouterTenant(provider_service=self, **kwargs)
        t.public_ip = ip
        t.public_mac = self.ip_to_mac(ip)
        t.address_pool_id = ap.id
        t.save()

        return t


class VRouterTenant(Tenant):
    class Meta:
        proxy = True
        verbose_name = "vRouter Tenant"

    KIND = VROUTER_KIND

    simple_attributes = (
        ("public_ip", None),
        ("public_mac", None),
        ("address_pool_id", None),
    )

    @property
    def gateway_ip(self):
        if not self.address_pool:
            return None
        return self.address_pool.gateway_ip

    @property
    def gateway_mac(self):
        if not self.address_pool:
            return None
        return self.address_pool.gateway_mac

    @property
    def cidr(self):
        if not self.address_pool:
            return None
        return self.address_pool.cidr

    @property
    def netbits(self):
        # return number of bits in the network portion of the cidr
        if self.cidr:
            parts = self.cidr.split("/")
            if len(parts) == 2:
                return int(parts[1].strip())
        return None

    @property
    def address_pool(self):
        if getattr(self, "cached_address_pool", None):
            return self.cached_address_pool
        if not self.address_pool_id:
            return None
        aps = AddressPool.objects.filter(id=self.address_pool_id)
        if not aps:
            return None
        ap = aps[0]
        self.cached_address_pool = ap
        return ap

    @address_pool.setter
    def address_pool(self, value):
        if value:
            value = value.id
        if (value != self.get_attribute("address_pool_id", None)):
            self.cached_address_pool = None
        self.set_attribute("address_pool_id", value)

    def cleanup_addresspool(self):
        if self.address_pool_id:
            ap = AddressPool.objects.filter(id=self.address_pool_id)
            if ap:
                ap[0].put_address(self.public_ip)
                self.public_ip = None

    def delete(self, *args, **kwargs):
        self.cleanup_addresspool()
        super(VRouterTenant, self).delete(*args, **kwargs)

VRouterTenant.setup_simple_attributes()


# DEVICES
class VRouterDevice(PlCoreBase):
    """define the information related to an device used by vRouter"""
    class Meta:
        app_label = APP_LABEL
        verbose_name = "vRouter Device"

    name = models.CharField(max_length=20, help_text="device friendly name", null=True, blank=True)
    openflow_id = models.CharField(max_length=20, help_text="device identifier in ONOS", null=False, blank=False)
    config_key = models.CharField(max_length=32, help_text="configuration key", null=False, blank=False, default="basic")
    driver = models.CharField(max_length=32, help_text="driver type", null=False, blank=False)
    vrouter_service = models.ForeignKey(VRouterService, related_name='devices')


# PORTS
class VRouterPort(PlCoreBase):
    class Meta:
        app_label = APP_LABEL
        verbose_name = "vRouter Port"

    name = models.CharField(max_length=20, help_text="port friendly name", null=True, blank=True)
    openflow_id = models.CharField(max_length=21, help_text="port identifier in ONOS", null=False, blank=False)
    vrouter_device = models.ForeignKey(VRouterDevice, related_name='ports')
    # NOTE probably is not meaningful to relate a port to a service
    vrouter_service = models.ForeignKey(VRouterService, related_name='device_ports')


class VRouterInterface(PlCoreBase):
    class Meta:
        app_label = APP_LABEL
        verbose_name = "vRouter Interface"

    name = models.CharField(max_length=20, help_text="interface friendly name", null=True, blank=True)
    vrouter_port = models.ForeignKey(VRouterPort, related_name='interfaces')
    name = models.CharField(max_length=10, help_text="interface name", null=False, blank=False)
    mac = models.CharField(max_length=17, help_text="interface mac", null=False, blank=False)
    vlan = models.CharField(max_length=10, help_text="interface vlan id", null=True, blank=True)


class VRouterIp(PlCoreBase):
    class Meta:
        app_label = APP_LABEL
        verbose_name = "vRouter Ip"

    name = models.CharField(max_length=20, help_text="ip friendly name", null=True, blank=True)
    vrouter_interface = models.ForeignKey(VRouterInterface, related_name='ips')
    ip = models.CharField(max_length=19, help_text="interface ips", null=False, blank=False)


# APPS
class VRouterApp(PlCoreBase):
    class Meta:
        app_label = "vrouter"
        verbose_name = "vRouter App"

    def _get_interfaces(self):
        app_interfaces = []
        devices = VRouterDevice.objects.filter(vrouter_service=self.vrouter_service)
        for device in devices:
            ports = VRouterPort.objects.filter(vrouter_device=device.id)
            for port in ports:
                interfaces = VRouterInterface.objects.filter(vrouter_port=port.id)
                for iface in interfaces:
                    app_interfaces.append(iface.name)
        return app_interfaces

    vrouter_service = models.ForeignKey(VRouterService, related_name='apps')
    name = models.CharField(max_length=50, help_text="application name", null=False, blank=False)
    control_plane_connect_point = models.CharField(max_length=21, help_text="port identifier in ONOS", null=False, blank=False)
    ospf_enabled = models.BooleanField(default=True, help_text="ospf enabled")
    interfaces = property(_get_interfaces)
