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
    mappings = _front_port_mappings(data, logger)

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
                rear_port, rear_position = item.get("rear_port"), item.get("rear_port_position")
                if rear_port is None:  # newer NetBox format: mapping lives in port-mappings
                    if item["name"] not in mappings:
                        raise ValueError(f"{data['model']}: front port '{item['name']}' has no rear port mapping.")
                    rear_port, rear_position = mappings[item["name"]]
                attrs["rear_port_template"] = _lookup(created, "rear-ports", rear_port, item, data)
                if rear_position is not None:
                    attrs["rear_port_position"] = rear_position

            created[section][item["name"]] = model.objects.create(**attrs)

        if debug_mode and created[section]:
            logger.info(f"{len(created[section])} {section} synced for {data['model']}.")


def _front_port_mappings(data, logger):
    """Read the NetBox `port-mappings` section as {front port name: (rear port name, rear position)}.

    Nautobot links each front port to a single rear port position. When NetBox maps one
    front port to several rear positions, the first mapping is used and a warning is logged.
    """
    result = {}
    for m in data.get("port-mappings") or []:
        front = m["front_port"]
        if front in result:
            logger.warning(
                f"{data['model']}: front port '{front}' has several rear mappings; "
                f"Nautobot supports one, keeping rear port '{result[front][0]}'."
            )
            continue
        result[front] = (m["rear_port"], m.get("rear_port_position"))
    return result


def _lookup(created, section, name, item, data):
    try:
        return created[section][name]
    except KeyError:
        raise ValueError(
            f"{data['model']}: '{item['name']}' references {section[:-1]} '{name}', which is not defined in the YAML."
        ) from None
