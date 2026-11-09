"""Optional toy chassis scenes, expressed entirely as ordinary sim/v1 data.

This module describes wiring. It owns no process, network, solver or clock.
Different machines and actuators can use the same Simulation without this preset.
"""

from pathlib import Path

CORNERS = ("fl", "fr", "rl", "rr")


def four_wheel_document(firmwares, model, *, ready=None, duration="50ms", quantum="1ms",
                        imus=True, referees=None, inputs=(), assertions=()):
    """Build one or two fixed toy chassis with one shared native world.

    ``firmwares`` preserves caller order and maps node names to ELF paths.
    The two-chassis model uses alpha/ and bravo/ native names respectively.
    Admission remains the responsibility of load_scene and registered factories.
    """
    if not isinstance(firmwares, dict) or not 1 <= len(firmwares) <= 2:
        raise ValueError("the fixed chassis preset requires one or two named firmwares")
    nodes, links, joints, sensors = {}, [], {}, {}
    ready = {} if ready is None else ready
    referees = {} if referees is None else referees
    if set(ready) - set(firmwares) or set(referees) - set(firmwares):
        raise ValueError("ready/referee configurations must name a declared machine")
    declarations, stimuli = [], list(inputs)
    for index, (name, firmware) in enumerate(firmwares.items()):
        prefix = ("alpha/", "bravo/")[index] if len(firmwares) == 2 else ""
        nodes[name] = {"kind": "machine.qemu", "firmware": str(Path(firmware).resolve()),
                       "uarts": ["uart1", "uart7"] if name in referees else ["uart1"]}
        bus = name + "_can"
        nodes[bus] = {"kind": "bus.can"}
        links.append([name + ".fdcan1", bus + ".can"])
        for motor_id, corner in enumerate(CORNERS, 1):
            motor, joint = name + "_motor_" + corner, prefix + "wheel_" + corner
            joints[joint] = prefix + "drive_" + corner
            nodes[motor] = {"kind": "device.dji-m3508", "motor_id": motor_id,
                            "plant": "joint", "joint": joint}
            links.extend([[motor + ".can", bus + ".can"], [motor + ".joint", "world.drives"]])
        if imus:
            sensors[name] = [prefix + "imu_gyro", prefix + "imu_accel"]
            net = name + "_imu"
            nodes[net] = {"kind": "net.imu"}
            links.extend([["world.imu." + name, net + ".signal"], [name + ".imu", net + ".signal"]])
        if name in ready:
            declarations.append({"name": name, "endpoint": name + ".uart1", "text": ready[name]})
        if name in referees:
            referee, serial = name + "_referee", name + "_referee_uart"
            nodes[referee] = {"kind": "device.referee", **referees[name]}
            nodes[serial] = {"kind": "net.uart"}
            links.extend([[referee + ".uart", serial + ".uart"], [name + ".uart7", serial + ".uart"]])
            stimuli.append({"at": "0ns", "endpoint": referee + ".enable", "value": True,
                            "origin": "marker:" + name if name in ready else "session"})
    nodes["world"] = {"kind": "physics.mujoco", "model": str(Path(model).resolve()),
                      "control": "bound", "joints": joints, "imus": sensors}
    return {"schema": "sim/v1", "runtime": {"quantum": quantum, "duration": duration},
            "nodes": nodes, "links": links,
            "experiment": {"ready": declarations, "inputs": stimuli, "assertions": list(assertions)}}


def add_can_faults(document, member, *, delays=(), feedback=()):
    """Compile explicit session-relative fault changes to existing CAN profiles.

    Delay affects future enqueues; feedback loss drops the selected motor's
    frames only on the route to its MCU. It does not power off that motor.
    Equal times apply delay changes before feedback changes, retaining order.
    """
    bus = member + "_can"
    if bus not in document["nodes"]:
        raise ValueError("CAN fault member is not in this scene")
    schedule = [(at, 0, index, "delay", value) for index, (at, value) in enumerate(delays)]
    schedule += [(at, 1, index, motor_id, enabled)
                 for index, (at, motor_id, enabled) in enumerate(feedback)]
    if any(type(at) is not int or at < 0 for at, *_ in schedule):
        raise ValueError("CAN fault offsets must be nonnegative integer nanoseconds")
    profiles, delay, blocked = {}, 0, set()
    for at, _, _, action, value in sorted(schedule):
        if action == "delay":
            if type(value) is not int or value < 0:
                raise ValueError("CAN delay must be nonnegative integer nanoseconds")
            delay = value
        else:
            if type(action) is not int or not 1 <= action <= 4 or type(value) is not bool:
                raise ValueError("feedback fault requires motor ID1..4 and boolean enabled")
            blocked.discard(action) if value else blocked.add(action)
        profile = {"delay_ns": delay, "drop": [
            {"source": member + "_motor_" + CORNERS[identifier - 1] + ".can",
             "destination": member + ".fdcan1", "can_id": 0x200 + identifier}
            for identifier in sorted(blocked)]}
        name = next((key for key, existing in profiles.items() if existing == profile), None)
        if name is None:
            if len(profiles) >= 16:
                raise ValueError("scene requires more than16 distinct CAN fault profiles")
            name = "policy" + str(len(profiles))
            profiles[name] = profile
        document["experiment"]["inputs"].append(
            {"at": str(at) + "ns", "endpoint": bus + ".fault", "value": name, "origin": "session"})
    if profiles:
        document["nodes"][bus]["fault_profiles"] = profiles
    return document
