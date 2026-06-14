"""The 41-generator vocabulary implementations (plugin-architecture §4).

Each generator is a deterministic function of the seeded draws it consumes from a
:class:`~dataforge_engine.behavior.rng.Cursor` (the ``values`` sub-seed; §7.3
fixed-draw accounting). The engine binds one :class:`GeneratorFn` per declared
attribute / payload generator at IR compile, consuming the **same**
``GENERATOR_CATALOG`` the validator uses (single-sourced contract). Multi-draw
generators (``template``, ``address.full``) consume their fixed per-call draw
count in declaration order (§7.3).

The cross-entity generators (``ref.fk``, ``ref.attr``) and the sibling-aware ones
(``person.email`` ``from``, ``template`` ``{attr}``, ``derived.expr``) read a
:class:`GenContext` the caller assembles. Money is :class:`~decimal.Decimal`
(rendered as a string by the serializer, S-6). Pure Python (BE-ENG-1).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING

from . import vocab
from .errors import CompileError, GenerationError
from .expr_eval import CompiledExpr, compile_expr

if TYPE_CHECKING:
    from dataforge_engine.envelope.types import JSONValue

    from .pools import EntityPools
    from .rng import Cursor


@dataclass
class GenContext:
    """The resolution context one generator call may read (plugin-architecture §4).

    * ``siblings`` — already-generated attributes of the same record (for
      ``person.email`` ``from``, ``template`` ``{attr}``, ``ref.attr`` ``via``).
    * ``pools`` — the entity pools (for ``ref.fk`` live-key selection / ``ref.attr``
      target lookup).
    * ``ref_keys`` — entity keys selected by ``ref.fk`` siblings this call may
      dereference (``ref.attr`` ``via`` resolves through here).
    * ``expr_resolver`` — resolves a ``derived.expr`` context path to a value
      (payload/effect context). ``None`` when no expressions are in scope.
    """

    siblings: dict[str, JSONValue] = field(default_factory=dict)
    pools: EntityPools | None = None
    ref_keys: dict[str, tuple[str, str]] = field(default_factory=dict)
    expr_resolver: Callable[[str], JSONValue] | None = None


# A bound generator: consumes draws from the cursor, reads the context, returns a
# JSON value (or a tuple of values for object generators handled by the caller).
GeneratorFn = Callable[["Cursor", GenContext], "JSONValue"]


def _int_param(params: Mapping[str, object], name: str, default: int = 0) -> int:
    value = params.get(name, default)
    if isinstance(value, bool):  # bool is an int subclass; treat as 0/1 explicitly
        return int(value)
    if isinstance(value, int | float | str):
        return int(value)
    return default


def _float_param(params: Mapping[str, object], name: str, default: float = 0.0) -> float:
    value = params.get(name, default)
    if isinstance(value, int | float | str):
        return float(value)
    return default


def _str_param(params: Mapping[str, object], name: str, default: str = "") -> str:
    value = params.get(name, default)
    return str(value) if value is not None else default


def _opt_float(params: Mapping[str, object], name: str) -> float | None:
    value = params.get(name)
    if isinstance(value, int | float | str):
        return float(value)
    return None


def _domains_param(params: Mapping[str, object], default: tuple[str, ...]) -> tuple[str, ...]:
    value = params.get("domains")
    if isinstance(value, list | tuple) and value:
        return tuple(str(v) for v in value)
    return default


def _scalar_options(params: Mapping[str, object]) -> list[JSONValue]:
    """``choice.uniform`` options: a list of scalars."""
    value = params.get("options")
    return list(value) if isinstance(value, list) else []


def _weighted_options(params: Mapping[str, object]) -> tuple[list[JSONValue], list[float]]:
    """``choice.weighted`` options: ``[{value, weight}]`` → (values, weights)."""
    raw = params.get("options")
    values: list[JSONValue] = []
    weights: list[float] = []
    if isinstance(raw, list):
        for opt in raw:
            if isinstance(opt, dict):
                values.append(opt.get("value"))
                w = opt.get("weight", 1)
                weights.append(float(w) if isinstance(w, int | float | str) else 1.0)
    return values, weights


def _pick(pool: tuple[str, ...], cursor: Cursor) -> str:
    return pool[cursor.u64() % len(pool)]


def _hex(cursor: Cursor, nibbles: int) -> str:
    return f"{cursor.u64() & ((1 << (4 * nibbles)) - 1):0{nibbles}x}"


def _digits(cursor: Cursor, count: int) -> str:
    value = cursor.u64() % (10**count)
    return f"{value:0{count}d}"


def _quantize(value: Decimal, scale: int) -> Decimal:
    return value.quantize(Decimal(10) ** -scale, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------
# Builder: GeneratorSpec name + params → GeneratorFn (bound at IR compile).
# ---------------------------------------------------------------------------


def build_generator(name: str, params: Mapping[str, object]) -> GeneratorFn:
    """Bind one declared generator to an executable closure.

    ``name`` is the catalog generator name; ``params`` are the (already
    L2-validated) manifest params. Raises :class:`CompileError` for a generator
    the engine does not implement (only ``hook`` — Phase deferred / gated).
    """
    builder = _BUILDERS.get(name)
    if builder is None:
        raise CompileError(f"generator {name!r} has no engine implementation")
    return builder(params)


# -- identity / person / address -------------------------------------------


def _build_id_uuid(_p: Mapping[str, object]) -> GeneratorFn:
    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        # UUIDv4-format from the seeded draw (deterministic; not the event_id UUIDv7).
        d = cursor.bytes32()[:16]
        h = d.hex()
        return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-{('89ab'[d[8] & 3])}{h[17:20]}-{h[20:32]}"
    return gen


def _build_id_seq(params: Mapping[str, object]) -> GeneratorFn:
    start = _int_param(params, "start", 1)
    step = _int_param(params, "step", 1)
    # Per-entity-type monotonic counter: keyed off the cursor position so it is
    # restorable and deterministic without a separate counter store.
    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        n = cursor.position
        cursor.position += 1
        return start + n * step
    return gen


def _build_first_name(_p: Mapping[str, object]) -> GeneratorFn:
    return lambda c, _ctx: _pick(vocab.FIRST_NAMES, c)


def _build_last_name(_p: Mapping[str, object]) -> GeneratorFn:
    return lambda c, _ctx: _pick(vocab.LAST_NAMES, c)


def _build_full_name(_p: Mapping[str, object]) -> GeneratorFn:
    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        return f"{_pick(vocab.FIRST_NAMES, cursor)} {_pick(vocab.LAST_NAMES, cursor)}"
    return gen


def _build_email(params: Mapping[str, object]) -> GeneratorFn:
    from_attr = params.get("from")
    domains = _domains_param(params, vocab.EMAIL_DOMAINS)

    def gen(cursor: Cursor, ctx: GenContext) -> JSONValue:
        domain = domains[cursor.u64() % len(domains)]
        if isinstance(from_attr, str) and from_attr in ctx.siblings:
            base = str(ctx.siblings[from_attr]).lower().replace(" ", ".")
            base = "".join(ch for ch in base if ch.isalnum() or ch == ".")
            suffix = _digits(cursor, 3)
            return f"{base}{suffix}@{domain}"
        local = f"{_pick(vocab.FIRST_NAMES, cursor).lower()}.{_digits(cursor, 4)}"
        return f"{local}@{domain}"
    return gen


def _build_username(params: Mapping[str, object]) -> GeneratorFn:
    from_attr = params.get("from")

    def gen(cursor: Cursor, ctx: GenContext) -> JSONValue:
        if isinstance(from_attr, str) and from_attr in ctx.siblings:
            base = str(ctx.siblings[from_attr]).lower().replace(" ", "_")
            base = "".join(ch for ch in base if ch.isalnum() or ch == "_")
            return f"{base}{_digits(cursor, 3)}"
        return f"{_pick(vocab.FIRST_NAMES, cursor).lower()}_{_digits(cursor, 4)}"
    return gen


def _build_phone(_p: Mapping[str, object]) -> GeneratorFn:
    return lambda c, _ctx: f"+1{_digits(c, 10)}"


def _build_address_street(_p: Mapping[str, object]) -> GeneratorFn:
    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        num = 1 + cursor.u64() % 9999
        return f"{num} {_pick(vocab.STREET_NAMES, cursor)} {_pick(vocab.STREET_SUFFIX, cursor)}"
    return gen


def _build_address_city(_p: Mapping[str, object]) -> GeneratorFn:
    return lambda c, _ctx: _pick(vocab.CITIES, c)


def _build_address_state(_p: Mapping[str, object]) -> GeneratorFn:
    return lambda c, _ctx: _pick(vocab.STATES, c)


def _build_address_postal(_p: Mapping[str, object]) -> GeneratorFn:
    return lambda c, _ctx: _digits(c, 5)


def _build_address_country(_p: Mapping[str, object]) -> GeneratorFn:
    return lambda c, _ctx: _pick(vocab.COUNTRIES, c)


def _build_address_full(_p: Mapping[str, object]) -> GeneratorFn:
    # The only nested-object source in v0 — its five properties draw in declared
    # order (§7.3 multi-draw fixed accounting).
    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        num = 1 + cursor.u64() % 9999
        street = f"{num} {_pick(vocab.STREET_NAMES, cursor)} {_pick(vocab.STREET_SUFFIX, cursor)}"
        return {
            "street": street,
            "city": _pick(vocab.CITIES, cursor),
            "state": _pick(vocab.STATES, cursor),
            "postal_code": _digits(cursor, 5),
            "country": _pick(vocab.COUNTRIES, cursor),
        }
    return gen


# -- commerce / internet / text --------------------------------------------


def _build_product_name(_p: Mapping[str, object]) -> GeneratorFn:
    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        return (
            f"{_pick(vocab.PRODUCT_ADJ, cursor)} "
            f"{_pick(vocab.PRODUCT_MATERIAL, cursor)} "
            f"{_pick(vocab.PRODUCT_NOUN, cursor)}"
        )
    return gen


def _build_category(params: Mapping[str, object]) -> GeneratorFn:
    depth = _int_param(params, "depth", 1)

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        parts = [_pick(vocab.CATEGORIES, cursor)]
        for _ in range(depth - 1):
            parts.append(_pick(vocab.CATEGORY_SUB, cursor))
        return "/".join(parts)
    return gen


def _build_brand(_p: Mapping[str, object]) -> GeneratorFn:
    return lambda c, _ctx: _pick(vocab.BRANDS, c)


def _build_sku(params: Mapping[str, object]) -> GeneratorFn:
    pattern = _str_param(params, "pattern", "{#hex8}")
    return _build_template({"pattern": pattern})


def _build_price(params: Mapping[str, object]) -> GeneratorFn:
    low = Decimal(_str_param(params, "min", "1.00"))
    high = Decimal(_str_param(params, "max", "999.99"))
    dist = _str_param(params, "distribution", "lognormal")

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        u = cursor.u()
        if dist == "uniform":
            value = low + (high - low) * Decimal(str(u))
        else:  # lognormal-shaped skew toward the low end
            skew = Decimal(str(u * u))
            value = low + (high - low) * skew
        return _quantize(value, 2)
    return gen


def _build_ip_v4(params: Mapping[str, object]) -> GeneratorFn:
    private = bool(params.get("private", False))

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        if private:
            return f"10.{cursor.u64() % 256}.{cursor.u64() % 256}.{1 + cursor.u64() % 254}"
        a = 1 + cursor.u64() % 223
        return f"{a}.{cursor.u64() % 256}.{cursor.u64() % 256}.{1 + cursor.u64() % 254}"
    return gen


def _build_user_agent(_p: Mapping[str, object]) -> GeneratorFn:
    return lambda c, _ctx: _pick(vocab.USER_AGENTS, c)


def _build_url(params: Mapping[str, object]) -> GeneratorFn:
    domains = _domains_param(params, vocab.URL_DOMAINS)

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        domain = domains[cursor.u64() % len(domains)]
        return f"https://{domain}/{_pick(vocab.WORDS, cursor)}/{_hex(cursor, 6)}"
    return gen


def _build_word(_p: Mapping[str, object]) -> GeneratorFn:
    return lambda c, _ctx: _pick(vocab.WORDS, c)


def _build_sentence(params: Mapping[str, object]) -> GeneratorFn:
    max_words = _int_param(params, "max_words", 12)

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        n = 3 + cursor.u64() % max(1, max_words - 2)
        words = [_pick(vocab.WORDS, cursor) for _ in range(n)]
        words[0] = words[0].capitalize()
        return " ".join(words) + "."
    return gen


def _build_paragraph(params: Mapping[str, object]) -> GeneratorFn:
    max_sentences = _int_param(params, "max_sentences", 3)
    sentence = _build_sentence({"max_words": 12})

    def gen(cursor: Cursor, ctx: GenContext) -> JSONValue:
        n = 1 + cursor.u64() % max(1, max_sentences)
        return " ".join(str(sentence(cursor, ctx)) for _ in range(n))
    return gen


# -- numeric / choice / time -----------------------------------------------


def _build_number_int(params: Mapping[str, object]) -> GeneratorFn:
    low = _int_param(params, "min")
    high = _int_param(params, "max")

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        span = high - low + 1
        return low + int(cursor.u64() % span)
    return gen


def _build_number_float(params: Mapping[str, object]) -> GeneratorFn:
    low = _float_param(params, "min")
    high = _float_param(params, "max")
    precision = _int_param(params, "precision", 2)

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        return round(low + cursor.u() * (high - low), precision)
    return gen


def _build_number_normal(params: Mapping[str, object]) -> GeneratorFn:
    from statistics import NormalDist
    mean = _float_param(params, "mean")
    stddev = _float_param(params, "stddev")
    lo = _opt_float(params, "min")
    hi = _opt_float(params, "max")
    precision = _int_param(params, "precision", 2)
    nd = NormalDist()

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        z = nd.inv_cdf(min(max(cursor.u(), 2.0**-64), 1.0 - 2.0**-53))
        value = mean + stddev * z
        if lo is not None:
            value = max(value, lo)
        if hi is not None:
            value = min(value, hi)
        return round(value, precision)
    return gen


def _build_number_lognormal(params: Mapping[str, object]) -> GeneratorFn:
    import math
    from statistics import NormalDist
    median = _float_param(params, "median")
    p95 = _float_param(params, "p95")
    lo = _opt_float(params, "min")
    hi = _opt_float(params, "max")
    precision = _int_param(params, "precision", 2)
    sigma = math.log(p95 / median) / 1.6448536269514722
    nd = NormalDist()

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        z = nd.inv_cdf(min(max(cursor.u(), 2.0**-64), 1.0 - 2.0**-53))
        value = math.exp(math.log(median) + sigma * z)
        if lo is not None:
            value = max(value, lo)
        if hi is not None:
            value = min(value, hi)
        return round(value, precision)
    return gen


def _build_number_zipf(params: Mapping[str, object]) -> GeneratorFn:
    n = _int_param(params, "n")
    s = _float_param(params, "s", 1.0)
    weights = [1.0 / (k**s) for k in range(1, n + 1)]
    total = sum(weights)
    cumulative: list[float] = []
    acc = 0.0
    for w in weights:
        acc += w / total
        cumulative.append(acc)

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        u = cursor.u()
        lo, hi = 0, len(cumulative) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if u <= cumulative[mid]:
                hi = mid
            else:
                lo = mid + 1
        return lo + 1
    return gen


def _build_number_decimal(params: Mapping[str, object]) -> GeneratorFn:
    low = Decimal(_str_param(params, "min"))
    high = Decimal(_str_param(params, "max"))
    scale = _int_param(params, "scale", 2)
    dist = _str_param(params, "distribution", "uniform")

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        u = cursor.u()
        if dist == "lognormal":
            frac = Decimal(str(u * u))
        elif dist == "normal":
            frac = Decimal(str(min(max(0.5 + (u - 0.5), 0.0), 1.0)))
        else:
            frac = Decimal(str(u))
        return _quantize(low + (high - low) * frac, scale)
    return gen


def _build_choice_weighted(params: Mapping[str, object]) -> GeneratorFn:
    values, weights = _weighted_options(params)
    total = sum(weights) or 1.0
    cumulative: list[float] = []
    acc = 0.0
    for w in weights:
        acc += w / total
        cumulative.append(acc)

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        u = cursor.u()
        for i, threshold in enumerate(cumulative):
            if u <= threshold:
                return values[i]
        return values[-1]
    return gen


def _build_choice_uniform(params: Mapping[str, object]) -> GeneratorFn:
    options = _scalar_options(params)

    def gen(cursor: Cursor, _ctx: GenContext) -> JSONValue:
        return options[cursor.u64() % len(options)]
    return gen


def _build_choice_boolean(params: Mapping[str, object]) -> GeneratorFn:
    p_true = _float_param(params, "p_true", 0.5)
    return lambda c, _ctx: c.u() < p_true


def _build_time_now(_p: Mapping[str, object]) -> GeneratorFn:
    # Resolved by the caller (it owns the virtual clock); the closure reads the
    # current virtual time the IR stamps into the context under "__now__".
    def gen(_cursor: Cursor, ctx: GenContext) -> JSONValue:
        value = ctx.siblings.get("__now__")
        if not isinstance(value, str):
            raise GenerationError("time.now requires the virtual clock in context")
        return value
    return gen


def _build_time_between(params: Mapping[str, object]) -> GeneratorFn:
    from .distributions import parse_duration_us
    start_us = parse_duration_us(_str_param(params, "start"))
    end_us = parse_duration_us(_str_param(params, "end"))

    def gen(cursor: Cursor, ctx: GenContext) -> JSONValue:
        epoch = ctx.siblings.get("__virtual_epoch_ms__")
        if not isinstance(epoch, int):
            raise GenerationError("time.between requires the virtual epoch in context")
        span = end_us - start_us
        offset_us = start_us + int(cursor.u64() % (span if span > 0 else 1))
        from .clock import format_simulated_ms
        return format_simulated_ms(epoch + offset_us // 1000)
    return gen


# -- template / references / derived ---------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{(#hex8|#hex16|#digits4|#digits8|#upper4|[a-z][a-z0-9_]*)\}")


def _build_template(params: Mapping[str, object]) -> GeneratorFn:
    pattern = _str_param(params, "pattern")

    def gen(cursor: Cursor, ctx: GenContext) -> JSONValue:
        def replace(match: re.Match[str]) -> str:
            token = match.group(1)
            if token == "#hex8":
                return _hex(cursor, 8)
            if token == "#hex16":
                return _hex(cursor, 16)
            if token == "#digits4":
                return _digits(cursor, 4)
            if token == "#digits8":
                return _digits(cursor, 8)
            if token == "#upper4":
                raw = _hex(cursor, 4).upper()
                return raw
            sibling = ctx.siblings.get(token)
            return str(sibling) if sibling is not None else ""
        return _PLACEHOLDER_RE.sub(replace, pattern)
    return gen


def _build_ref_fk(params: Mapping[str, object]) -> GeneratorFn:
    relationship = _str_param(params, "relationship")
    selection = _str_param(params, "selection", "uniform")
    s = _float_param(params, "s", 1.0)

    def gen(cursor: Cursor, ctx: GenContext) -> JSONValue:
        if ctx.pools is None:
            raise GenerationError("ref.fk requires pools in context")
        target = ctx.pools.relationship_target(relationship)
        keys = ctx.pools.live_keys(target)
        if not keys:
            raise GenerationError(f"ref.fk {relationship}: no live {target} entities")
        if selection == "zipf":
            idx = _zipf_index(cursor.u(), len(keys), s)
        elif selection == "recent":
            # recent window: bias toward the tail (most recently created keys).
            span = max(1, len(keys) // 4)
            idx = len(keys) - 1 - int(cursor.u64() % span)
        else:
            idx = int(cursor.u64() % len(keys))
        chosen = keys[idx]
        return chosen

    # Tag the closure with its relationship so a sibling ref.attr (via=this attr)
    # can resolve the selected entity (evaluate._register_ref).
    gen.__df_relationship__ = relationship  # type: ignore[attr-defined]
    return gen


def _zipf_index(u: float, n: int, s: float) -> int:
    weights = [1.0 / (k**s) for k in range(1, n + 1)]
    total = sum(weights)
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w / total
        if u <= acc:
            return i
    return n - 1


def _build_ref_attr(params: Mapping[str, object]) -> GeneratorFn:
    via = _str_param(params, "via")
    attribute = _str_param(params, "attribute")

    def gen(_cursor: Cursor, ctx: GenContext) -> JSONValue:
        ref = ctx.ref_keys.get(via)
        if ref is None:
            raise GenerationError(f"ref.attr via {via!r}: no resolved fk in context")
        entity_type, entity_key = ref
        if ctx.pools is None:
            raise GenerationError("ref.attr requires pools in context")
        record = ctx.pools.require(entity_type, entity_key)
        return record.attributes[attribute]
    return gen


def _build_derived_expr(params: Mapping[str, object]) -> GeneratorFn:
    compiled: CompiledExpr = compile_expr(_str_param(params, "expr"))
    output = _str_param(params, "output")
    scale = _int_param(params, "scale", 2)

    def gen(_cursor: Cursor, ctx: GenContext) -> JSONValue:
        resolver = ctx.expr_resolver
        if resolver is None:
            raise GenerationError("derived.expr requires an expr resolver in context")
        value = compiled.evaluate(resolver)
        if output == "integer":
            return int(value)
        if output == "decimal":
            return _quantize(value, scale)
        return float(value)
    return gen


_BUILDERS: dict[str, Callable[[Mapping[str, object]], GeneratorFn]] = {
    "id.uuid": _build_id_uuid,
    "id.seq": _build_id_seq,
    "person.first_name": _build_first_name,
    "person.last_name": _build_last_name,
    "person.full_name": _build_full_name,
    "person.email": _build_email,
    "person.username": _build_username,
    "person.phone": _build_phone,
    "address.street": _build_address_street,
    "address.city": _build_address_city,
    "address.state": _build_address_state,
    "address.postal_code": _build_address_postal,
    "address.country": _build_address_country,
    "address.full": _build_address_full,
    "commerce.product_name": _build_product_name,
    "commerce.category": _build_category,
    "commerce.brand": _build_brand,
    "commerce.sku": _build_sku,
    "commerce.price": _build_price,
    "internet.ip_v4": _build_ip_v4,
    "internet.user_agent": _build_user_agent,
    "internet.url": _build_url,
    "text.word": _build_word,
    "text.sentence": _build_sentence,
    "text.paragraph": _build_paragraph,
    "number.int": _build_number_int,
    "number.float": _build_number_float,
    "number.normal": _build_number_normal,
    "number.lognormal": _build_number_lognormal,
    "number.zipf": _build_number_zipf,
    "number.decimal": _build_number_decimal,
    "choice.weighted": _build_choice_weighted,
    "choice.uniform": _build_choice_uniform,
    "choice.boolean": _build_choice_boolean,
    "time.now": _build_time_now,
    "time.between": _build_time_between,
    "template": _build_template,
    "ref.fk": _build_ref_fk,
    "ref.attr": _build_ref_attr,
    "derived.expr": _build_derived_expr,
}
"""generator name → builder. ``hook`` is intentionally absent (gated; the
reference scenario uses zero hooks — P-4)."""
