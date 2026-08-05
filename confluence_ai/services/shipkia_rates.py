from __future__ import annotations

import csv
import hashlib
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any


RATE_CARD_VERSION = "Rate Card 10 - June"
RATE_CARD_SOURCE_FILENAME = "Latest June Shipkia Default rate card - Rate Card 10 (22).csv"
RATE_CARD_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "shipkia_rate_card_10_june.csv"
)
GST_RATE = Decimal("18")
VOLUMETRIC_DIVISOR = Decimal("5000")
ZONES = ("A", "B", "C", "D", "E", "F")
STARTING_RATE_WEIGHT_G = Decimal("500")
GENERAL_STARTING_RATE = Decimal("22")
FLAT_RATE_COURIER = "E-Kart"
FLAT_RATE_SERVICE = "E-Kart SURFACE"
FLAT_ZONAL_RATE_COURIER = "E-Kart"
FLAT_ZONAL_RATE_SERVICE = "E-Kart EXPRESS"
EXPECTED_FLAT_RATE_SLABS = (
    (Decimal("0"), Decimal("500")),
    (Decimal("501"), Decimal("1000")),
    (Decimal("1001"), Decimal("2000")),
)


@dataclass(frozen=True)
class RateRow:
    courier_partner: str
    service: str
    movement: str
    min_weight: str
    max_weight_g: Decimal
    zone_prices: dict[str, Decimal]
    cod_minimum: Decimal
    cod_percentage: Decimal
    dph_divisor: Decimal

    @property
    def is_additional(self) -> bool:
        return self.min_weight == "+"

    @property
    def min_weight_g(self) -> Decimal:
        return Decimal("0") if self.is_additional else Decimal(self.min_weight)


def calculate_rate(arguments: dict[str, Any]) -> dict[str, Any]:
    """Calculate customer-facing ShipKia rates deterministically from Rate Card 10."""
    try:
        dead_weight_kg = _dead_weight_kg(arguments)
        length, width, height = _dimensions(arguments)
        payment_type = _payment_type(arguments)
        movement = _movement(arguments)
        zone = _zone(arguments)
        order_value = _optional_decimal(arguments.get("order_value"), "order_value")
    except ValueError as exc:
        return {"status": "validation_error", "eligible_rates": [], "message": str(exc)}

    if payment_type == "COD" and order_value is not None and order_value < 0:
        return {
            "status": "validation_error",
            "eligible_rates": [],
            "message": "COD order value cannot be negative.",
        }

    volumetric_weight_kg = (
        (length * width * height / VOLUMETRIC_DIVISOR)
        if length is not None and width is not None and height is not None
        else None
    )
    chargeable_weight_kg = max(
        dead_weight_kg,
        volumetric_weight_kg if volumetric_weight_kg is not None else Decimal("0"),
    )
    chargeable_weight_g = (chargeable_weight_kg * Decimal("1000")).to_integral_value(
        rounding=ROUND_CEILING
    )
    if chargeable_weight_g <= 0:
        return {
            "status": "validation_error",
            "eligible_rates": [],
            "message": "Shipment weight must be greater than zero.",
        }

    all_rows = load_rate_card()
    requested_courier = str(arguments.get("courier") or "").strip()
    requested_service = str(arguments.get("service") or "").strip()
    requested_transport_mode = str(arguments.get("mode") or "").strip()
    candidates = _service_rates(
        all_rows,
        movement=movement,
        chargeable_weight_g=chargeable_weight_g,
        payment_type=payment_type,
        order_value=order_value,
        zone=zone,
        courier_filter=requested_courier,
        service_filter=requested_service,
        transport_mode=requested_transport_mode,
    )

    requested_selection_unavailable = bool(
        (requested_courier or requested_service or requested_transport_mode) and not candidates
    )
    candidates.sort(key=_sort_key)
    flat_rate_options = [
        _flat_rate_option_summary(rate)
        for rate in candidates
        if rate["is_flat_rate"]
    ][:3]
    flat_additional_rate_options = sorted(
        (
            _flat_additional_rate_option_summary(rate)
            for rate in candidates
            if rate["additional_rate_is_flat"]
        ),
        key=_flat_additional_rate_sort_key,
    )[:3]
    eligible_rates = candidates[:3]
    all_eligible_rates_flat = bool(eligible_rates) and all(
        rate["is_flat_rate"] for rate in eligible_rates
    )
    card = rate_card_metadata()
    response: dict[str, Any] = {
        "status": (
            "requested_service_unavailable"
            if requested_selection_unavailable
            else ("success" if eligible_rates else "no_eligible_rate")
        ),
        "rate_card": card,
        "movement_type": movement,
        "payment_type": payment_type,
        "dead_weight_kg": _number(dead_weight_kg, 3),
        "volumetric_weight_kg": (
            _number(volumetric_weight_kg, 3) if volumetric_weight_kg is not None else None
        ),
        "chargeable_weight_kg": _number(chargeable_weight_kg, 3),
        "chargeable_weight_g": int(chargeable_weight_g),
        "dimensions_used": volumetric_weight_kg is not None,
        "zone": zone,
        "zone_required": (
            zone is None
            and not requested_selection_unavailable
            and not all_eligible_rates_flat
        ),
        "cod_order_value_required": payment_type == "COD" and order_value is None,
        "requested_selection": {
            "courier": requested_courier or None,
            "service": requested_service or None,
            "transport_mode": requested_transport_mode or None,
        },
        "requested_service_unavailable": requested_selection_unavailable,
        "preferred_courier_unavailable": bool(requested_courier and not candidates),
        "exact_service_unavailable": bool(requested_service and not candidates),
        "transit_time_available": False,
        "transit_time_note": (
            "The active rate card has prices but no delivery-time or SLA data. Do not describe "
            "an Air or Express-labelled option as the fastest or promise a delivery time."
        ),
        "flat_rate_available": bool(flat_rate_options),
        "flat_rate_options": flat_rate_options,
        "flat_additional_rate_available": bool(flat_additional_rate_options),
        "flat_additional_rate_options": flat_additional_rate_options,
        "eligible_rates": eligible_rates,
    }

    if requested_selection_unavailable:
        response["available_services_for_requested_courier"] = _configured_services(
            all_rows,
            movement=movement,
            courier_filter=requested_courier,
        )
        response["available_services_for_requested_service"] = _configured_services(
            all_rows,
            movement=movement,
            service_filter=requested_service,
        )
        response["available_services_for_requested_mode"] = _configured_services(
            all_rows,
            movement=movement,
            transport_mode=requested_transport_mode,
        )
        requested_label = " + ".join(
            value
            for value in (requested_courier, requested_service, requested_transport_mode)
            if value
        )
        response["message"] = (
            f"{requested_label} does not have a matching non-zero rate in the active ShipKia "
            "rate card for these details. Tell the customer that the requested service is not "
            "available in the current rate card. Do not rename a Standard service as Express, "
            "and do not quote unrelated alternatives unless the customer asks for them."
        )
    elif not eligible_rates:
        response["message"] = (
            "No non-zero rate is configured for this movement, weight and courier selection. "
            "Do not invent a rate."
        )
    elif zone is None and all_eligible_rates_flat:
        response["message"] = (
            "Every returned option has one verified amount across complete Zones A-F. Present the "
            "flat_rate_breakdown without an approved-zone qualification."
        )
    elif zone is None:
        response["message"] = (
            "The rate card does not map pincodes to zones. Give only a qualified starting price "
            "from the returned Zone A-F amounts and say the exact price depends on the approved "
            "zone. Do not ask the customer to identify an internal zone and do not invent one."
        )
    elif response["cod_order_value_required"]:
        response["message"] = (
            "Present the shipping charge and COD formula, then ask only for the COD order value."
        )
    else:
        response["message"] = (
            "Present up to these three lowest-priced eligible options. Amounts include 18% GST "
            "only where a total is returned."
        )

    if _is_speed_request(requested_transport_mode) and not requested_selection_unavailable:
        response["speed_selection_note"] = (
            "These options come only from active rate-card service names containing Air or "
            "Express. They are not verified as fastest because the rate card has no transit SLA."
        )

    if flat_rate_options:
        response["flat_rate_note"] = (
            "flat_rate_options contains only services whose complete Zone A-F amount breakdown is "
            "identical. Use these options when the customer explicitly asks for a flat rate; do "
            "not describe any other option as flat."
        )

    if flat_additional_rate_options:
        response["flat_additional_rate_note"] = (
            "flat_additional_rate_options contains additional-weight charges that are identical "
            "across complete Zones A-F. These are not complete flat shipment rates because the "
            "base charge can still depend on the approved zone. State the configured base slab "
            "and the verified per-unit additional charge."
        )

    if volumetric_weight_kg is None:
        response["weight_note"] = (
            "Calculated using dead weight because dimensions were not supplied; dimensions can "
            "change the final chargeable weight."
        )
    return response


def get_starting_rate(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a non-customer-specific starting-rate headline or approved-zone floor."""
    arguments = arguments or {}
    raw_zone = str(arguments.get("zone") or "").strip().upper()
    zone = raw_zone.removeprefix("ZONE").strip()
    if zone not in ZONES:
        zone = ""
    courier_filter = str(arguments.get("courier_partner") or "").strip()
    transport_mode = str(arguments.get("transport_mode") or "").strip()

    if not zone:
        return {
            "status": "success",
            "response_type": "general_starting",
            "zone": None,
            "amount": _money(GENERAL_STARTING_RATE),
            "currency": "INR",
            "gst_inclusive": False,
            "marketing_headline": True,
            "rate_card": rate_card_metadata(),
            "message": (
                "ShipKia shipping rates start from Rs 22. The exact rate depends on the route, "
                "weight and service."
            ),
        }

    eligible_rows = [
        row
        for row in load_rate_card()
        if (
            row.movement == "FWD"
            and not row.is_additional
            and row.min_weight_g <= STARTING_RATE_WEIGHT_G <= row.max_weight_g
            and row.zone_prices[zone] > 0
            and (not courier_filter or _matches_courier(row, courier_filter))
            and (not transport_mode or _matches_transport_mode(row, transport_mode))
        )
    ]
    if not eligible_rows:
        return {
            "status": "configuration_required",
            "response_type": "zone_starting",
            "zone": zone,
            "amount": None,
            "currency": "INR",
            "gst_inclusive": True,
            "rate_card": rate_card_metadata(),
            "message": f"No verified starting rate is configured for Zone {zone}.",
        }

    selected = min(
        eligible_rows,
        key=lambda row: (row.zone_prices[zone], row.service.casefold()),
    )
    base_amount = selected.zone_prices[zone]
    gst = base_amount * GST_RATE / Decimal("100")
    total = base_amount + gst
    return {
        "status": "success",
        "response_type": "zone_starting",
        "zone": zone,
        "amount": _money(total),
        "currency": "INR",
        "gst_inclusive": True,
        "marketing_headline": False,
        "basis": {
            "movement_type": "Forward",
            "weight_slab_g": int(STARTING_RATE_WEIGHT_G),
            "courier": selected.courier_partner,
            "service": selected.service,
            "base_amount": _money(base_amount),
            "gst": _money(gst),
        },
        "requested_courier_partner": courier_filter or None,
        "requested_transport_mode": transport_mode or None,
        "rate_card": rate_card_metadata(),
        "message": (
            f"Zone {zone} shipping rates start from Rs {_money(total):.2f}, including GST."
        ),
    }


def get_flat_rates(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the three verified all-zone E-Kart Surface flat-rate slabs."""
    return _get_flat_rates_impl(arguments or {})


def _get_flat_rates_impl(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_scope = str(arguments.get("response_scope") or "").strip().casefold()
    scope_map = {
        "": "Matching" if _has_weight(arguments) else "Starting",
        "starting": "Starting",
        "matching": "Matching",
        "all": "All",
    }
    return _get_flat_rates_with_scope(arguments, raw_scope, scope_map)


def get_flat_zonal_rates(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the verified E-Kart Express Flat-Zonal catalog.

    Flat-Zonal is distinct from all-zone Flat pricing: one complete base rate
    applies to Zones A-B and another applies to Zones C-F. The additional
    500-gram component is verified separately and is never presented as a
    complete shipment price.
    """
    arguments = arguments or {}
    payment_arguments = dict(arguments)
    payment_arguments["payment_type"] = arguments.get("payment_type") or "Prepaid"
    try:
        payment_type = _payment_type(payment_arguments)
        order_value = _optional_decimal(arguments.get("order_value"), "order_value")
    except ValueError as exc:
        return _flat_zonal_rate_error("validation_error", str(exc))

    if order_value is not None and order_value <= 0:
        return _flat_zonal_rate_error(
            "validation_error",
            "COD order value must be greater than zero.",
        )
    if payment_type == "COD" and order_value is None:
        return {
            **_flat_zonal_rate_error(
                "order_value_required",
                "Ask only for the COD order value before quoting the Flat-Zonal rate.",
            ),
            "payment_type": "COD",
            "cod_order_value_required": True,
        }

    try:
        base_row, additional_row = _verified_flat_zonal_rate_rows()
    except (FileNotFoundError, ValueError) as exc:
        return _flat_zonal_rate_error("configuration_required", str(exc))

    groups = (
        ("A-B", ("A", "B")),
        ("C-F", ("C", "D", "E", "F")),
    )
    zone_groups = []
    for label, zones in groups:
        breakdown = _amount_breakdown(
            shipping=base_row.zone_prices[zones[0]],
            payment_type=payment_type,
            order_value=order_value,
            cod_minimum=base_row.cod_minimum,
            cod_percentage=base_row.cod_percentage,
        )
        zone_groups.append(
            {
                "zone_group": label,
                "zones": list(zones),
                "min_weight_g": int(base_row.min_weight_g),
                "max_weight_g": int(base_row.max_weight_g),
                "payment_type": payment_type,
                **breakdown,
            }
        )

    additional_breakdown = _amount_breakdown(
        shipping=additional_row.zone_prices[ZONES[0]],
        payment_type="Prepaid",
        order_value=None,
        cod_minimum=Decimal("0"),
        cod_percentage=Decimal("0"),
    )
    return {
        "status": "success",
        "response_type": "flat_zonal_all",
        "currency": "INR",
        "gst_inclusive": True,
        "movement_type": "Forward",
        "payment_type": payment_type,
        "order_value": _money(order_value) if order_value is not None else None,
        "cod_order_value_required": False,
        "courier_partner": FLAT_ZONAL_RATE_COURIER,
        "service": FLAT_ZONAL_RATE_SERVICE,
        "zone_groups": zone_groups,
        "additional_weight": {
            "applies_after_weight_g": int(base_row.max_weight_g),
            "additional_weight_unit_g": int(additional_row.max_weight_g),
            **additional_breakdown,
        },
        "rate_card": rate_card_metadata(),
        "message": (
            "Speak both verified GST-inclusive E-Kart Express Flat-Zonal base-rate groups, "
            "then the verified additional 500-gram condition."
        ),
    }


def _verified_flat_zonal_rate_rows() -> tuple[RateRow, RateRow]:
    rows = tuple(
        row
        for row in load_rate_card()
        if row.courier_partner == FLAT_ZONAL_RATE_COURIER
        and row.service == FLAT_ZONAL_RATE_SERVICE
        and row.movement == "FWD"
    )
    base_rows = [row for row in rows if not row.is_additional]
    additional_rows = [row for row in rows if row.is_additional]
    if len(base_rows) != 1 or len(additional_rows) != 1:
        raise ValueError(
            "The active rate card does not contain the expected E-Kart Express Flat-Zonal rows."
        )
    base_row = base_rows[0]
    additional_row = additional_rows[0]
    if base_row.min_weight_g != 0 or base_row.max_weight_g != Decimal("500"):
        raise ValueError("The E-Kart Express Flat-Zonal base slab changed.")
    ab_price = base_row.zone_prices["A"]
    cf_price = base_row.zone_prices["C"]
    if (
        ab_price <= 0
        or cf_price <= 0
        or ab_price == cf_price
        or any(base_row.zone_prices[zone] != ab_price for zone in ("A", "B"))
        or any(base_row.zone_prices[zone] != cf_price for zone in ("C", "D", "E", "F"))
    ):
        raise ValueError("The E-Kart Express Zone A-B / Zone C-F Flat-Zonal structure changed.")
    additional_price = additional_row.zone_prices["A"]
    if additional_price <= 0 or any(
        additional_row.zone_prices[zone] != additional_price for zone in ZONES[1:]
    ):
        raise ValueError("The E-Kart Express additional 500-gram Flat-Zonal rate changed.")
    return base_row, additional_row


def _flat_zonal_rate_error(status: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "response_type": "flat_zonal_unavailable",
        "currency": "INR",
        "gst_inclusive": True,
        "zone_groups": [],
        "message": message,
    }


def _get_flat_rates_with_scope(
    arguments: dict[str, Any],
    raw_scope: str,
    scope_map: dict[str, str],
) -> dict[str, Any]:
    if raw_scope not in scope_map:
        return _flat_rate_error(
            "validation_error",
            "response_scope must be Starting, Matching or All.",
        )
    response_scope = scope_map[raw_scope]

    payment_arguments = dict(arguments)
    payment_arguments["payment_type"] = arguments.get("payment_type") or "Prepaid"
    try:
        payment_type = _payment_type(payment_arguments)
        order_value = _optional_decimal(arguments.get("order_value"), "order_value")
    except ValueError as exc:
        return _flat_rate_error("validation_error", str(exc))

    if order_value is not None and order_value <= 0:
        return _flat_rate_error(
            "validation_error",
            "COD order value must be greater than zero.",
        )
    if payment_type == "COD" and order_value is None:
        return {
            **_flat_rate_error(
                "order_value_required",
                "Ask only for the COD order value before quoting the verified flat rate.",
            ),
            "payment_type": "COD",
            "cod_order_value_required": True,
            "response_scope": response_scope,
        }

    try:
        rows = _verified_flat_rate_rows()
    except (FileNotFoundError, ValueError) as exc:
        return _flat_rate_error("configuration_required", str(exc))

    all_options = [
        _flat_catalog_option(
            row,
            payment_type=payment_type,
            order_value=order_value,
        )
        for row in rows
    ]
    starting_option = all_options[0]
    chargeable_weight_g: Decimal | None = None
    matching_option: dict[str, Any] | None = None
    if response_scope == "Matching":
        if not _has_weight(arguments):
            return _flat_rate_error(
                "validation_error",
                "A positive shipment weight is required for a matching flat-rate slab.",
            )
        try:
            dead_weight_kg = _dead_weight_kg(arguments)
            length, width, height = _dimensions(arguments)
        except ValueError as exc:
            return _flat_rate_error("validation_error", str(exc))
        volumetric_weight_kg = (
            length * width * height / VOLUMETRIC_DIVISOR
            if length is not None and width is not None and height is not None
            else None
        )
        chargeable_weight_kg = max(
            dead_weight_kg,
            volumetric_weight_kg if volumetric_weight_kg is not None else Decimal("0"),
        )
        chargeable_weight_g = (
            chargeable_weight_kg * Decimal("1000")
        ).to_integral_value(rounding=ROUND_CEILING)
        matching_option = next(
            (
                option
                for option in all_options
                if Decimal(str(option["min_weight_g"]))
                <= chargeable_weight_g
                <= Decimal(str(option["max_weight_g"]))
            ),
            None,
        )

    if response_scope == "All":
        returned_options = all_options
        response_type = "flat_all"
        message = "Speak the three verified GST-inclusive E-Kart Surface flat-rate slabs."
    elif response_scope == "Matching" and matching_option is not None:
        returned_options = [matching_option]
        response_type = "flat_matching"
        message = "Speak only the verified GST-inclusive flat rate for this chargeable weight."
    elif response_scope == "Matching":
        returned_options = [starting_option]
        response_type = "flat_starting_fallback"
        message = (
            "No exact flat slab matches this chargeable weight. Speak only the verified flat-rate "
            "starting headline and do not imply that it applies to this shipment."
        )
    else:
        returned_options = [starting_option]
        response_type = "flat_starting"
        message = "Speak only the verified GST-inclusive flat-rate starting headline."

    return {
        "status": "success",
        "response_type": response_type,
        "response_scope": response_scope,
        "currency": "INR",
        "gst_inclusive": True,
        "movement_type": "Forward",
        "payment_type": payment_type,
        "order_value": _money(order_value) if order_value is not None else None,
        "cod_order_value_required": False,
        "courier_partner": FLAT_RATE_COURIER,
        "service": FLAT_RATE_SERVICE,
        "chargeable_weight_g": (
            int(chargeable_weight_g) if chargeable_weight_g is not None else None
        ),
        "exact_match_available": matching_option is not None,
        "starting_flat_rate": starting_option,
        "flat_rate_options": returned_options,
        "verified_flat_rate_count": len(all_options),
        "excluded_additional_weight_components": True,
        "rate_card": rate_card_metadata(),
        "message": message,
    }


def _verified_flat_rate_rows() -> tuple[RateRow, ...]:
    rows = tuple(
        sorted(
            (
                row
                for row in load_rate_card()
                if row.courier_partner == FLAT_RATE_COURIER
                and row.service == FLAT_RATE_SERVICE
                and row.movement == "FWD"
                and not row.is_additional
            ),
            key=lambda row: (row.min_weight_g, row.max_weight_g),
        )
    )
    actual_slabs = tuple((row.min_weight_g, row.max_weight_g) for row in rows)
    if actual_slabs != EXPECTED_FLAT_RATE_SLABS:
        raise ValueError(
            "The active rate card does not contain exactly the three expected E-Kart Surface "
            "flat-rate slabs. Do not quote a flat amount."
        )
    for row in rows:
        prices = tuple(row.zone_prices[zone] for zone in ZONES)
        if prices[0] <= 0 or any(price != prices[0] for price in prices[1:]):
            raise ValueError(
                "An E-Kart Surface slab is not a positive complete Zone A-F flat rate. "
                "Do not quote a flat amount."
            )
        if row.cod_minimum != 0 or row.cod_percentage != 0:
            raise ValueError(
                "The E-Kart Surface flat-rate COD configuration changed. Do not quote a flat "
                "amount until the new rule is reviewed."
            )
    return rows


def _flat_catalog_option(
    row: RateRow,
    *,
    payment_type: str,
    order_value: Decimal | None,
) -> dict[str, Any]:
    breakdown = _amount_breakdown(
        shipping=row.zone_prices[ZONES[0]],
        payment_type=payment_type,
        order_value=order_value,
        cod_minimum=row.cod_minimum,
        cod_percentage=row.cod_percentage,
    )
    return {
        "courier_partner": row.courier_partner,
        "service": row.service,
        "min_weight_g": int(row.min_weight_g),
        "max_weight_g": int(row.max_weight_g),
        "payment_type": payment_type,
        "shipping_charge": breakdown["shipping_charge"],
        "cod_charge": breakdown["cod_charge"],
        "gst": breakdown["gst"],
        "total": breakdown["total"],
    }


def _flat_rate_error(status: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "response_type": "flat_unavailable",
        "currency": "INR",
        "gst_inclusive": True,
        "flat_rate_options": [],
        "message": message,
    }


def _has_weight(arguments: dict[str, Any]) -> bool:
    return any(
        arguments.get(key) not in (None, "")
        for key in ("dead_weight", "dead_weight_kg", "dead_weight_g")
    )


@lru_cache(maxsize=1)
def load_rate_card() -> tuple[RateRow, ...]:
    if not RATE_CARD_PATH.exists():
        raise FileNotFoundError(f"ShipKia rate card is missing: {RATE_CARD_PATH}")

    rows: list[RateRow] = []
    with RATE_CARD_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            cleaned = {str(key or "").strip(): str(value or "").strip() for key, value in raw.items()}
            rows.append(
                RateRow(
                    courier_partner=cleaned["Courier Partner"],
                    service=cleaned["Couriers"],
                    movement=cleaned["Mode"].upper(),
                    min_weight=cleaned["Min weight"],
                    max_weight_g=_decimal(cleaned["Max weight"], "Max weight"),
                    zone_prices={
                        zone: _decimal(cleaned[f"Zone {zone}"], f"Zone {zone}") for zone in ZONES
                    },
                    cod_minimum=_decimal(cleaned["COD Amount"], "COD Amount"),
                    cod_percentage=_decimal(cleaned["COD Percentage"], "COD Percentage"),
                    dph_divisor=_decimal(cleaned.get("DPH Divisor", "0"), "DPH Divisor"),
                )
            )
    if not rows:
        raise ValueError("ShipKia rate card contains no pricing rows.")
    return tuple(rows)


@lru_cache(maxsize=1)
def rate_card_metadata() -> dict[str, Any]:
    content = RATE_CARD_PATH.read_bytes()
    rows = load_rate_card()
    return {
        "version": RATE_CARD_VERSION,
        "source_filename": RATE_CARD_SOURCE_FILENAME,
        "sha256": hashlib.sha256(content).hexdigest(),
        "row_count": len(rows),
        "gst_rate": 18,
    }


def _service_rates(
    rows: tuple[RateRow, ...],
    *,
    movement: str,
    chargeable_weight_g: Decimal,
    payment_type: str,
    order_value: Decimal | None,
    zone: str | None,
    courier_filter: str = "",
    service_filter: str = "",
    transport_mode: str = "",
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[RateRow]] = {}
    for row in rows:
        if row.movement != movement:
            continue
        if courier_filter and not _matches_courier(row, courier_filter):
            continue
        if service_filter and not _matches_service(row, service_filter):
            continue
        if transport_mode and not _matches_transport_mode(row, transport_mode):
            continue
        grouped.setdefault((row.courier_partner, row.service), []).append(row)

    rates = []
    for service_rows in grouped.values():
        calculated = _calculate_service(
            service_rows,
            chargeable_weight_g=chargeable_weight_g,
            payment_type=payment_type,
            order_value=order_value,
            zone=zone,
        )
        if calculated:
            rates.append(calculated)
    return rates


def _configured_services(
    rows: tuple[RateRow, ...],
    *,
    movement: str,
    courier_filter: str = "",
    service_filter: str = "",
    transport_mode: str = "",
) -> list[str]:
    services = {
        row.service
        for row in rows
        if row.movement == movement
        and (not courier_filter or _matches_courier(row, courier_filter))
        and (not service_filter or _matches_service(row, service_filter))
        and (not transport_mode or _matches_transport_mode(row, transport_mode))
    }
    return sorted(services)


def _calculate_service(
    rows: list[RateRow],
    *,
    chargeable_weight_g: Decimal,
    payment_type: str,
    order_value: Decimal | None,
    zone: str | None,
) -> dict[str, Any] | None:
    base_rows = [row for row in rows if not row.is_additional]
    additional_rows = [row for row in rows if row.is_additional]
    selected_row: RateRow | None = None
    additional_row: RateRow | None = None
    additional_units = 0

    base_zero = sorted(
        (row for row in base_rows if row.min_weight_g == 0),
        key=lambda row: row.max_weight_g,
    )
    if additional_rows and base_zero:
        selected_row = base_zero[0]
        additional_row = sorted(additional_rows, key=lambda row: row.max_weight_g)[0]
        if chargeable_weight_g > selected_row.max_weight_g:
            additional_units = math.ceil(
                (chargeable_weight_g - selected_row.max_weight_g) / additional_row.max_weight_g
            )
    else:
        containing = [
            row
            for row in base_rows
            if row.min_weight_g <= chargeable_weight_g <= row.max_weight_g
        ]
        if containing:
            selected_row = min(containing, key=lambda row: row.max_weight_g)

    if selected_row is None:
        return None

    all_zone_breakdowns: dict[str, dict[str, Any]] = {}
    for selected_zone in ZONES:
        shipping = selected_row.zone_prices[selected_zone]
        if additional_units and additional_row:
            shipping += Decimal(additional_units) * additional_row.zone_prices[selected_zone]
        if shipping == 0:
            continue
        all_zone_breakdowns[selected_zone] = _amount_breakdown(
            shipping=shipping,
            payment_type=payment_type,
            order_value=order_value,
            cod_minimum=selected_row.cod_minimum,
            cod_percentage=selected_row.cod_percentage,
        )

    if not all_zone_breakdowns or (zone and zone not in all_zone_breakdowns):
        return None

    flat_rate_breakdown = _verified_flat_rate_breakdown(all_zone_breakdowns)
    flat_additional_rate_breakdown = _verified_flat_additional_rate_breakdown(additional_row)
    result: dict[str, Any] = {
        "courier_partner": selected_row.courier_partner,
        "service": selected_row.service,
        "pricing_structure": "base_plus_additional" if additional_row else "explicit_weight_band",
        "base_weight_g": int(selected_row.max_weight_g),
        "additional_weight_unit_g": int(additional_row.max_weight_g) if additional_row else None,
        "additional_units": additional_units,
        "cod_minimum": _money(selected_row.cod_minimum),
        "cod_percentage": _number(selected_row.cod_percentage, 3),
        "dph_divisor_metadata": _number(selected_row.dph_divisor, 3),
        "dph_included_in_charge": False,
        "is_flat_rate": flat_rate_breakdown is not None,
        "flat_rate_breakdown": flat_rate_breakdown,
        "additional_rate_is_flat": flat_additional_rate_breakdown is not None,
        "flat_additional_rate_breakdown": flat_additional_rate_breakdown,
    }
    if zone:
        result.update(all_zone_breakdowns[zone])
    else:
        result["zone_breakdowns"] = all_zone_breakdowns
    return result


def _verified_flat_rate_breakdown(
    breakdowns: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if set(breakdowns) != set(ZONES):
        return None
    first = breakdowns[ZONES[0]]
    if any(breakdowns[zone] != first for zone in ZONES[1:]):
        return None
    return dict(first)


def _flat_rate_option_summary(rate: dict[str, Any]) -> dict[str, Any]:
    return {
        "courier_partner": rate["courier_partner"],
        "service": rate["service"],
        "pricing_structure": rate["pricing_structure"],
        "base_weight_g": rate["base_weight_g"],
        "additional_weight_unit_g": rate["additional_weight_unit_g"],
        "additional_units": rate["additional_units"],
        "is_flat_rate": True,
        "flat_rate_breakdown": rate["flat_rate_breakdown"],
    }


def _verified_flat_additional_rate_breakdown(
    additional_row: RateRow | None,
) -> dict[str, Any] | None:
    if additional_row is None:
        return None
    shipping = additional_row.zone_prices[ZONES[0]]
    if shipping <= 0 or any(
        additional_row.zone_prices[zone] != shipping for zone in ZONES[1:]
    ):
        return None
    return _amount_breakdown(
        shipping=shipping,
        payment_type="Prepaid",
        order_value=None,
        cod_minimum=Decimal("0"),
        cod_percentage=Decimal("0"),
    )


def _flat_additional_rate_option_summary(rate: dict[str, Any]) -> dict[str, Any]:
    return {
        "courier_partner": rate["courier_partner"],
        "service": rate["service"],
        "pricing_structure": rate["pricing_structure"],
        "applies_after_weight_g": rate["base_weight_g"],
        "additional_weight_unit_g": rate["additional_weight_unit_g"],
        "additional_rate_is_flat": True,
        "flat_additional_rate_breakdown": rate["flat_additional_rate_breakdown"],
    }


def _flat_additional_rate_sort_key(option: dict[str, Any]) -> tuple[float, str]:
    breakdown = option["flat_additional_rate_breakdown"]
    return (
        float(breakdown["shipping_charge"]),
        str(option["service"]).casefold(),
    )


def _amount_breakdown(
    *,
    shipping: Decimal,
    payment_type: str,
    order_value: Decimal | None,
    cod_minimum: Decimal,
    cod_percentage: Decimal,
) -> dict[str, Any]:
    if payment_type == "COD" and order_value is None:
        return {
            "shipping_charge": _money(shipping),
            "cod_charge": None,
            "cod_formula": f"max({_money(cod_minimum)}, order value x {_number(cod_percentage, 3)}%)",
            "gst": None,
            "total": None,
        }

    cod_charge = Decimal("0")
    if payment_type == "COD":
        percentage_charge = (order_value or Decimal("0")) * cod_percentage / Decimal("100")
        cod_charge = max(cod_minimum, percentage_charge)
    subtotal = shipping + cod_charge
    gst = subtotal * GST_RATE / Decimal("100")
    return {
        "shipping_charge": _money(shipping),
        "cod_charge": _money(cod_charge),
        "gst": _money(gst),
        "total": _money(subtotal + gst),
    }


def _dead_weight_kg(arguments: dict[str, Any]) -> Decimal:
    if arguments.get("dead_weight_g") not in (None, ""):
        value = _decimal(arguments["dead_weight_g"], "dead_weight_g") / Decimal("1000")
    else:
        raw = arguments.get("dead_weight_kg", arguments.get("dead_weight"))
        value = _decimal(raw, "dead_weight")
        unit = str(arguments.get("weight_unit") or "kg").strip().lower()
        if unit in {"g", "gram", "grams"}:
            value /= Decimal("1000")
        elif unit not in {"kg", "kilogram", "kilograms"}:
            raise ValueError("weight_unit must be kg or g.")
    if value <= 0:
        raise ValueError("A dead weight greater than zero is required.")
    return value


def _dimensions(arguments: dict[str, Any]) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    values = [arguments.get(key) for key in ("length", "width", "height")]
    provided = [value not in (None, "") for value in values]
    if any(provided) and not all(provided):
        raise ValueError("Provide length, width and height together, in centimetres.")
    if not any(provided):
        return None, None, None
    dimensions = tuple(_decimal(value, "dimension") for value in values)
    if any(value <= 0 for value in dimensions):
        raise ValueError("Package dimensions must be greater than zero.")
    return dimensions


def _payment_type(arguments: dict[str, Any]) -> str:
    value = str(arguments.get("payment_type") or "").strip().upper()
    if value in {"PREPAID", "PRE-PAID", "PAID"}:
        return "Prepaid"
    if value in {"COD", "CASH ON DELIVERY"}:
        return "COD"
    raise ValueError("payment_type must be Prepaid or COD.")


def _movement(arguments: dict[str, Any]) -> str:
    value = str(arguments.get("movement_type") or "FWD").strip().upper()
    mapping = {
        "FWD": "FWD",
        "FORWARD": "FWD",
        "FORWARD SHIPPING": "FWD",
        "RTO": "RTO",
        "RETURN TO ORIGIN": "RTO",
        "DTO": "DTO",
        "REVERSE": "DTO",
        "REVERSE SHIPPING": "DTO",
    }
    if value not in mapping:
        raise ValueError("movement_type must be Forward, RTO or DTO.")
    return mapping[value]


def _zone(arguments: dict[str, Any]) -> str | None:
    value = str(arguments.get("zone") or arguments.get("shipping_zone") or "").strip().upper()
    value = value.removeprefix("ZONE").strip()
    if not value:
        return None
    if value not in ZONES:
        raise ValueError("zone must be A, B, C, D, E or F.")
    return value


def _matches_courier(row: RateRow, requested: str) -> bool:
    needle = _normal_text(requested)
    return needle in _normal_text(row.courier_partner) or needle in _normal_text(row.service)


def _matches_service(row: RateRow, requested: str) -> bool:
    """Match only an exact service label from the active rate card."""
    return _normal_text(row.service) == _normal_text(requested)


def _matches_transport_mode(row: RateRow, requested: str) -> bool:
    mode = _normal_text(requested)
    service = _normal_text(row.service)
    if mode in {"surface", "ground"}:
        return "surface" in service
    if mode in {"air", "air express"}:
        return "air" in service
    if mode in {"express", "express delivery"}:
        return "express" in service
    if _is_speed_request(mode):
        return "air" in service or "express" in service
    return mode in service


def _is_speed_request(requested: str) -> bool:
    return _normal_text(requested) in {
        "fast",
        "fast delivery",
        "fastest",
        "fastest delivery",
        "priority",
        "priority delivery",
    }


def _sort_key(rate: dict[str, Any]) -> tuple[Decimal, str]:
    if "shipping_charge" in rate:
        amount = Decimal(str(rate.get("total") or rate["shipping_charge"]))
    else:
        zone_values = [
            Decimal(str(breakdown.get("total") or breakdown["shipping_charge"]))
            for breakdown in rate["zone_breakdowns"].values()
        ]
        amount = sum(zone_values, Decimal("0")) / Decimal(len(zone_values))
    return amount, str(rate["service"])


def _normal_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _optional_decimal(value: Any, label: str) -> Decimal | None:
    return None if value in (None, "") else _decimal(value, label)


def _decimal(value: Any, label: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{label} must be a valid number.") from None


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _number(value: Decimal, places: int) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(value.quantize(quantum, rounding=ROUND_HALF_UP))
