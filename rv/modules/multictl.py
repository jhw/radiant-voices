from enum import Enum
from itertools import chain

from rv.chunks import ArrayChunk
from rv.controller import Controller, Range, CompactRange
from rv.errors import MappingError
from rv.modules import Behavior as B, Module


def convert_value(gain, qsteps, smin, smax, dmin, dmax, vmax, value, curve=None):
    value = (value * gain) / 256
    value = min(value, 32768)
    if curve is not None:
        bucket = int(value / 128)
        start = 128 * bucket
        offset = value - start
        b = curve[bucket]
        a = curve[bucket + 1] if bucket < 256 else b
        c = min(offset / 128, 1.0)
        value = int((c * a) + ((1.0 - c) * b))
    srange = smax - smin
    if qsteps < 32768:
        quant = max(qsteps - 1, 1)
        step = 32768 / quant
        value = int(value / step)
        value = (value * step) / 32768
        value = smin + int(srange * value)
    else:
        value = smin + (srange * value) // 32768
    # TODO: out_offset
    drange = dmax - dmin
    if vmax is not None:
        value /= 32768 / vmax
    if drange > 0:
        value += dmin
    else:
        value = dmin - value
    return int(value)


def invert_value(gain, smin, smax, dmin, dmax, vmax, value):
    drange = dmax - dmin
    if drange > 0:
        value -= dmin
    else:
        value = dmin + value
    value *= drange
    # TODO: out_offset
    # TODO: map using multictl curve
    if gain == 0:
        return 0
    srange = smax - smin if smax > smin else smin - smax
    if srange == 0:
        return 0
    value *= 32768 / vmax
    value -= smin
    value /= srange
    value = min(32768, value)
    value = max(0, value)
    value *= 256
    value /= gain
    if smin >= smax:
        value = 32768 - value
    return int(value)


class MultiCtl(Module):

    name = mtype = "MultiCtl"
    mgroup = "Misc"
    chnk = 2
    flags = 0x020051

    behaviors = {B.sends_controls}

    class Mapping:
        def __init__(self, value):
            self.min, self.max, self.controller = value[:3]

    class MappingArray(ArrayChunk):
        chnm = 0
        length = 16
        type = "IIIIIIII"
        element_size = 4 * 8

        def default(self, _):
            return MultiCtl.Mapping((0, 0x8000, 0))

        @property
        def encoded_values(self):
            return list(
                chain.from_iterable(
                    (x.min, x.max, x.controller, 0, 0, 0, 0, 0) for x in self.values
                )
            )

        @property
        def python_type(self):
            return MultiCtl.Mapping

    class Curve(ArrayChunk):
        chnm = 1
        length = 257
        type = "H"
        element_size = 2
        min_value = 0
        max_value = 0x8000

        def default(self, x):
            return x * 0x80

    value = Controller((0, 32768), 0)
    gain = Controller((0, 1024), 256)
    quantization = Controller((0, 32768), 32768)
    out_offset = Controller((-16384, 16384), 0)
    response = Controller((0, 1000), 1000)
    sample_rate_hz = Controller((1, 32768), 150)

    def __init__(self, **kwargs):
        curve = kwargs.pop("curve", None)
        mappings = kwargs.pop("mappings", [])
        super(MultiCtl, self).__init__(**kwargs)
        self.curve = self.Curve()
        if curve is not None:
            self.curve.values = curve
        self.mappings = self.MappingArray()
        for i, mapping in enumerate(mappings):
            self.mappings.values[i] = self.Mapping(mapping)

    def on_value_changed(self, value, down, up):
        if self.parent is not None and down:
            downstream_mods = []
            for to_mod in range(256):
                from_mods = self.parent.module_connections[to_mod]
                if self.index in from_mods:
                    downstream_mods.append(to_mod)
            for i, to_mod in enumerate(downstream_mods):
                mapping = self.mappings.values[i]
                mod = self.parent.modules[to_mod]
                ctl = list(mod.controllers.values())[mapping.controller - 1]
                vt = ctl.value_type
                if isinstance(vt, Range):
                    if isinstance(vt, CompactRange):
                        vmax = None
                    else:
                        vmax = vt.max - vt.min
                    smin, smax = mapping.min, mapping.max
                    dmin, dmax = 0, vt.max - vt.min
                    if smin > smax:
                        smin, smax = smax, smin
                        dmin, dmax = dmax, dmin
                    converted = convert_value(
                        self.gain,
                        self.quantization,
                        smin,
                        smax,
                        dmin,
                        dmax,
                        vmax,
                        self.value,
                        self.curve.values,
                    )
                    final_value = converted + vt.min
                    setattr(mod, ctl.name, final_value)
                # TODO: apply out_offset
                # TODO: what should we do if it's not a range?

    def reflect(self, index=0, propagate=True):
        """Reflect the value of the controller mapped at the given index; inverse of setting value"""
        downstream_mods = []
        for to_mod in range(256):
            from_mods = self.parent.module_connections[to_mod]
            if self.index in from_mods:
                downstream_mods.append(to_mod)
                if len(downstream_mods) == index + 1:
                    break
        else:
            raise IndexError("No destination module mapped at index {}".format(index))
        mapping = self.mappings.values[index]
        if mapping.controller == 0:
            raise IndexError(
                "No destination controller mapped at index {}".format(index)
            )
        reflect_mod = self.parent.modules[downstream_mods[-1]]
        reflect_ctl_name = list(reflect_mod.controllers)[mapping.controller - 1]
        reflect_ctl = reflect_mod.controllers[reflect_ctl_name]
        reflect_value = getattr(reflect_mod, reflect_ctl_name)
        if hasattr(reflect_value, "value"):
            reflect_value = reflect_value.value
        t = reflect_ctl.value_type
        if isinstance(t, Range):
            dmin = t.min
            dmax = t.max
        elif t is bool:
            dmin = 0
            dmax = 1
        elif isinstance(t, type) and issubclass(t, Enum):
            dmin = 0
            dmax = len(t)
        else:
            dmin = 0
            dmax = 32768
        inverted = invert_value(
            gain=self.gain,
            smin=mapping.min,
            smax=mapping.max,
            dmin=dmin,
            dmax=dmax,
            vmax=dmax - dmin if dmax > dmin else dmin - dmax,
            value=reflect_value,
        )
        if propagate:
            self.value = inverted
        else:
            self.controller_values["value"] = inverted

    def specialized_iff_chunks(self):
        for chunk in self.mappings.chunks():
            yield chunk
        for chunk in self.curve.chunks():
            yield chunk
        for chunk in super(MultiCtl, self).specialized_iff_chunks():
            yield chunk

    def load_chunk(self, chunk):
        if chunk.chnm == 0:
            self.mappings.bytes = chunk.chdt
        if chunk.chnm == 1:
            self.curve.bytes = chunk.chdt

    @staticmethod
    def macro(project, *mod_ctl_pairs, name=None, layer=0, x=0, y=0, initial=None):
        if len(mod_ctl_pairs) > 16:
            raise MappingError("MultiCtl supports max of 16 destinations")
        mappings = []
        mods = []
        gains = set()
        for mod, ctl in mod_ctl_pairs:
            if not isinstance(ctl, Controller):
                ctl = mod.controllers[ctl]
            t = ctl.instance_value_type(mod)
            if isinstance(t, type) and issubclass(t, Enum):
                mapmin, mapmax = 0, len(t) - 1
                gains.add(256 + int(256 / mapmax))
            elif t is bool:
                mapmin, mapmax = 0, 1
                gains.add(512)
            elif t.min == 1:
                mapmin, mapmax = t.min, t.max
                gains.add(256 + int(256 / mapmax))
            elif isinstance(t, CompactRange):
                mapmin, mapmax = 0, (t.max - t.min)
                gains.add(256)
            else:
                mapmin, mapmax = 0, 0x8000
                gains.add(256)
            mappings.append((mapmin, mapmax, ctl.number))
            mods.append(project.modules[mod.index])
        if len(mods) != len(set(mods)):
            raise MappingError(
                "Only one MultiCtl mapping per destination module allowed"
            )
        if gains and len(gains) == 1:
            gain = list(gains).pop()
        else:
            gain = 256
        bundle = project.new_module(
            MultiCtl, name=name, layer=layer, x=x, y=y, gain=gain, mappings=mappings
        )
        bundle >> mods
        if initial is not None:
            bundle.value = initial
        return bundle
