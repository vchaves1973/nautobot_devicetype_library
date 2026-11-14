"""Shared helpers to sync component templates from device/module type YAML data.

Used by both the device type and module type import jobs. No Job classes here.
"""

from nautobot.dcim.models import (
    ConsolePortTemplate,
    ConsoleServerPortTemplate,
    DeviceBayTemplate,
    FrontPortTemplate,
    InterfaceTemplate,
    PowerOutletTemplate,
    PowerPortTemplate,
    RearPortTemplate,
)

try:  # ModuleBayTemplate only exists in Nautobot >= 2.3
    from nautobot.dcim.models import ModuleBayTemplate
except ImportError:  # pragma: no cover - depends on Nautobot version
    ModuleBayTemplate = None


# (yaml section, model, YAML keys copied as-is)
# Order matters: power ports before outlets, rear ports before front ports.
SIMPLE_FIELDS = ["name", "type", "label", "description"]
COMPONENTS = [
    ("interfaces", InterfaceTemplate, SIMPLE_FIELDS + ["mgmt_only"]),
    ("console-ports", ConsolePortTemplate, SIMPLE_FIELDS),
    ("console-server-ports", ConsoleServerPortTemplate, SIMPLE_FIELDS),
    ("power-ports", PowerPortTemplate, SIMPLE_FIELDS + ["maximum_draw", "allocated_draw", "power_factor"]),
    ("power-outlets", PowerOutletTemplate, SIMPLE_FIELDS + ["feed_leg"]),
    ("rear-ports", RearPortTemplate, SIMPLE_FIELDS + ["positions"]),
    ("front-ports", FrontPortTemplate, SIMPLE_FIELDS + ["rear_port_position"]),
    ("module-bays", ModuleBayTemplate, ["name", "position", "label", "description"]),
    ("device-bays", DeviceBayTemplate, ["name", "label", "description"]),
]

# Sections only valid on device types (DeviceBayTemplate has no module_type FK).
DEVICE_ONLY = {"device-bays"}


def sync_component_templates(parent, data, parent_field, logger, debug_mode=False):
    """Replace all component templates of `parent` with those defined in `data`.

    parent_field is "device_type" or "module_type". Run inside a transaction so a
    failure does not leave a half-populated type behind.
    """
    created = {}  # section -> {name: template}, used to resolve FK references

    for section, model, fields in COMPONENTS:
        if parent_field != "device_type" and section in DEVICE_ONLY:
            continue
        if model is None:
            if data.get(section):
                logger.warning(f"Skipping {section} for {data['model']}: not supported by this Nautobot version.")
            continue

        model.objects.filter(**{parent_field: parent}).delete()
        created[section] = {}

        for item in data.get(section) or []:
            attrs = {f: item[f] for f in fields if item.get(f) is not None}
            attrs[parent_field] = parent

            if section == "power-outlets" and item.get("power_port"):
                attrs["power_port_template"] = _lookup(created, "power-ports", item["power_port"], item, data)
            if section == "front-ports":
                attrs["rear_port_template"] = _lookup(created, "rear-ports", item["rear_port"], item, data)

            created[section][item["name"]] = model.objects.create(**attrs)

        if debug_mode and created[section]:
            logger.info(f"{len(created[section])} {section} synced for {data['model']}.")


def _lookup(created, section, name, item, data):
    try:
        return created[section][name]
    except KeyError:
        raise ValueError(
            f"{data['model']}: '{item['name']}' references {section[:-1]} '{name}', which is not defined in the YAML."
        ) from None
