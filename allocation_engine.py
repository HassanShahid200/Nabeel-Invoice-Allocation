"""Original V7 allocation and export logic, unchanged; no desktop UI."""
from __future__ import annotations

import csv
import math
import random
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import List, Optional, Sequence
from xml.sax.saxutils import escape


DEFAULT_MAX_AMOUNT = Decimal("25000")
DEFAULT_MIN_AMOUNT = Decimal("10000")

# Known account names from your sample. If the account name matches one of
# these, the account code can still be filled automatically for legacy two-column CSVs.
# New three-column CSVs can provide Account Code directly.
KNOWN_ACCOUNTS = {
    "travelling": ("C10106051", "CP for travelling expenses"),
    "uniforms": ("C10106052", "CP for uniform expenses"),
    "printing & stationery": ("C10106053", "CP for printing & stationery expenses"),
    "repair & maintenance": ("C10106054", "CP for repair & maintenance expenses"),
    "local supplies": ("C10106055", "CP for local supplies expenses"),
    "kitchen supplies": ("C10106056", "CP for kitchen expenses"),
    "container supplies": ("C10106057", "CP for container supplies expenses"),
    "misc expenses": ("C10106058", "CP for misc expenses"),
    "cleaning & sanitation": ("C10106059", "CP for cleaning & sanitation expenses"),
    "entertainment expenses": ("C10106060", "CP for entertainment expenses"),
    "other expenses": ("C10106061", "CP for other expenses"),
}


@dataclass(frozen=True)
class AccountInput:
    code: str
    name: str
    percentage: Decimal
    order: int


@dataclass(frozen=True)
class Account:
    code: str
    name: str
    percentage: Decimal
    target: Decimal
    narration: str
    order: int


@dataclass(frozen=True)
class SourceData:
    start_date: date
    end_date: date
    total_amount: Decimal
    accounts: List[Account]
    decimals: int


@dataclass(frozen=True)
class AllocationRow:
    allocation_date: date
    account: Account
    amount: Decimal


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def parse_money(value: str) -> Decimal:
    s = _text(value).replace(",", "").replace(" ", "")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s:
        raise ValueError("Total Amount is required.")
    try:
        result = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"Cannot read amount '{value}'.")
    if result <= 0:
        raise ValueError("Amount must be greater than zero.")
    return result


def parse_percent(value: str) -> Optional[Decimal]:
    s = _text(value)
    if not s:
        return None
    try:
        if s.endswith("%"):
            pct = Decimal(s[:-1].strip()) / Decimal("100")
        else:
            n = Decimal(s)
            pct = n if n <= 1 else n / Decimal("100")
    except InvalidOperation:
        return None
    return pct


def parse_date(value: str) -> date:
    s = _text(value)
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(
        f"Cannot read date '{value}'. Use DD/MM/YYYY, DD-MM-YYYY or YYYY-MM-DD."
    )


def decimal_places(value: Decimal) -> int:
    return min(max(0, -value.as_tuple().exponent), 2)


def to_units(value: Decimal, decimals: int) -> int:
    scale = Decimal(10) ** decimals
    return int((value * scale).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_units(units: int, decimals: int) -> Decimal:
    return Decimal(units) / (Decimal(10) ** decimals)


def normalize_account_name(name: str) -> str:
    s = _text(name).lower()
    s = s.replace("expesnes", "expenses")
    s = re.sub(r"\s+e$", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def account_code_and_narration(name: str) -> tuple[str, str]:
    key = normalize_account_name(name)
    if key in KNOWN_ACCOUNTS:
        return KNOWN_ACCOUNTS[key]
    return "", f"CP for {key}"


def read_accounts_csv(path: Path) -> List[AccountInput]:
    """Read Account Code / Account Name / Percentage CSV.

    Legacy two-column Account Name / Percentage CSVs are also accepted;
    for those, known account codes are filled from the built-in map.
    """
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    if not rows:
        raise ValueError("CSV is empty.")

    header = [_text(x).lower() for x in rows[0]]
    code_aliases = {"account code", "acct code", "acct. code", "code"}
    name_aliases = {"account name", "account", "name"}
    pct_aliases = {"percentage", "percent", "%", "pct"}

    code_col = next((i for i, x in enumerate(header) if x in code_aliases), None)
    name_col = next((i for i, x in enumerate(header) if x in name_aliases), None)
    pct_col = next((i for i, x in enumerate(header) if x in pct_aliases), None)

    data_start = 1
    if name_col is None or pct_col is None:
        # Also accept a two-column legacy file with no header.
        if len(rows[0]) >= 2 and parse_percent(rows[0][1]) is not None:
            code_col, name_col, pct_col, data_start = None, 0, 1, 0
        else:
            raise ValueError(
                "CSV must contain Account Code, Account Name and Percentage.\n"
                "Example header: Account Code,Account Name,Percentage"
            )

    accounts: List[AccountInput] = []
    for row_number, row in enumerate(rows[data_start:], start=data_start + 1):
        if not row or all(not _text(x) for x in row):
            continue
        required_cols = [name_col, pct_col] + ([code_col] if code_col is not None else [])
        if max(required_cols) >= len(row):
            raise ValueError(f"CSV row {row_number} does not contain all required columns.")

        name = _text(row[name_col])
        pct = parse_percent(row[pct_col])
        code = _text(row[code_col]) if code_col is not None else ""

        if not name:
            raise ValueError(f"CSV row {row_number}: Account Name is blank.")
        if pct is None or pct <= 0:
            raise ValueError(
                f"CSV row {row_number}: Percentage '{row[pct_col]}' is invalid."
            )

        # For legacy files without a code column, use the built-in map when possible.
        if not code:
            code = account_code_and_narration(name)[0]

        accounts.append(AccountInput(code, name, pct, len(accounts)))

    if not accounts:
        raise ValueError("No account rows were found in the CSV.")

    total_pct = sum((a.percentage for a in accounts), Decimal("0"))
    if abs(total_pct - Decimal("1")) > Decimal("0.0001"):
        raise ValueError(
            "Percentages must add to 100%. Current total is "
            f"{(total_pct * 100).quantize(Decimal('0.01'))}%."
        )

    return accounts


def build_source(
    accounts_csv: Path,
    total_amount: Decimal,
    start_date: date,
    end_date: date,
) -> SourceData:
    if end_date < start_date:
        raise ValueError("End Date cannot be before Start Date.")

    inputs = read_accounts_csv(accounts_csv)
    decimals = decimal_places(total_amount)
    total_units = to_units(total_amount, decimals)

    pct_sum = sum((a.percentage for a in inputs), Decimal("0"))
    weights = [a.percentage / pct_sum for a in inputs]

    raw_units = [Decimal(total_units) * pct for pct in weights]
    floor_units = [
        int(x.to_integral_value(rounding=ROUND_DOWN))
        for x in raw_units
    ]
    remaining = total_units - sum(floor_units)

    remainder_order = sorted(
        range(len(raw_units)),
        key=lambda i: (raw_units[i] - Decimal(floor_units[i]), -i),
        reverse=True,
    )
    target_units = floor_units[:]
    for i in remainder_order[:remaining]:
        target_units[i] += 1

    accounts: List[Account] = []
    for inp, units in zip(inputs, target_units):
        _mapped_code, narration = account_code_and_narration(inp.name)
        code = inp.code or _mapped_code
        accounts.append(
            Account(
                code=code,
                name=inp.name,
                percentage=inp.percentage,
                target=from_units(units, decimals),
                narration=narration,
                order=inp.order,
            )
        )

    if sum(to_units(a.target, decimals) for a in accounts) != total_units:
        raise RuntimeError("Internal error: account targets do not match Total Amount.")

    return SourceData(start_date, end_date, total_amount, accounts, decimals)


def split_varied_exact_amount(
    target: Decimal,
    max_amount: Decimal,
    min_amount: Decimal,
    decimals: int,
    rng: random.Random,
    used_units: set[int],
    prefer_unique: bool,
) -> List[Decimal]:
    """Fast exact split with good visual variation.

    This version avoids the expensive V6 search loop. Every selection is made
    inside a mathematically feasible interval, so only a small number of
    candidates are needed. Unique values and varied endings are preferred,
    but exact totals and Min/Max constraints always take priority.
    """
    target_units = to_units(target, decimals)
    max_units = to_units(max_amount, decimals)
    min_units = to_units(min_amount, decimals)

    if max_units <= 0:
        raise ValueError("Maximum amount must be greater than zero.")
    if min_units <= 0:
        raise ValueError("Minimum amount must be greater than zero.")
    if min_units > max_units:
        raise ValueError("Minimum amount cannot be greater than Maximum amount.")
    if target_units <= 0:
        return []

    min_count = math.ceil(target_units / max_units)
    max_count = target_units // min_units

    effective_min = min_units
    if max_count < min_count:
        if target_units <= max_units:
            return [from_units(target_units, decimals)]
        effective_min = 1
        max_count = target_units // effective_min

    midpoint = (effective_min + max_units) / 2
    ideal_count = max(1, round(target_units / midpoint))
    ideal_count = max(min_count, min(max_count, ideal_count))

    # A small set of nearby row counts gives visible variation without the
    # large retry cost used by V6.
    counts = list(range(max(min_count, ideal_count - 2), min(max_count, ideal_count + 2) + 1))
    rng.shuffle(counts)
    if ideal_count in counts:
        counts.remove(ideal_count)
        counts.insert(0, ideal_count)

    base = 10 ** decimals

    def ending(value: int) -> int:
        whole = value // base if base else value
        return whole % 100

    # Try only a handful of complete constructions. Feasibility bounds at each
    # step guarantee that the remaining amount can always fill later slots.
    for count in counts:
        for _attempt in range(24):
            remaining = target_units
            parts: List[int] = []
            local_used: set[int] = set()
            local_endings: set[int] = set()
            failed = False

            for i in range(count - 1):
                slots_left = count - i - 1
                low = max(effective_min, remaining - slots_left * max_units)
                high = min(max_units, remaining - slots_left * effective_min)
                if low > high:
                    failed = True
                    break

                # Twelve candidates are enough because every candidate is already
                # feasible. Score uniqueness, ending variety and spread.
                candidates = {low, high, (low + high) // 2}
                for _ in range(9):
                    candidates.add(rng.randint(low, high))

                best_value = None
                best_score = None
                used_endings = {ending(v) for v in used_units}
                center = (low + high) // 2

                for value in candidates:
                    duplicate = value in used_units or value in local_used
                    if prefer_unique and duplicate:
                        continue
                    end = ending(value)
                    ending_repeat = end in used_endings or end in local_endings
                    # Favor values away from the local midpoint and with fresh
                    # endings. A random tie-break avoids mechanical patterns.
                    spread_score = abs(value - center)
                    score = (
                        0 if duplicate else 1,
                        0 if ending_repeat else 1,
                        spread_score,
                        rng.random(),
                    )
                    if best_score is None or score > best_score:
                        best_score = score
                        best_value = value

                if best_value is None:
                    failed = True
                    break

                parts.append(best_value)
                local_used.add(best_value)
                local_endings.add(ending(best_value))
                remaining -= best_value

            if failed:
                continue

            last = remaining
            if not (effective_min <= last <= max_units):
                continue
            if prefer_unique and (last in used_units or last in local_used):
                continue

            parts.append(last)
            if sum(parts) != target_units or any(v > max_units for v in parts):
                continue

            rng.shuffle(parts)
            used_units.update(parts)
            return [from_units(x, decimals) for x in parts]

    raise ValueError(
        "Could not create varied unique amounts with the selected range. "
        "Try lowering Minimum amount or turn off 'Prefer unique amounts'."
    )

def distributed_dates(start: date, end: date, count: int) -> List[date]:
    if count <= 0:
        return []
    days = (end - start).days + 1
    if count == 1:
        return [start]
    if days == 1:
        return [start] * count

    result: List[date] = []
    for i in range(count):
        offset = round(i * (days - 1) / (count - 1))
        result.append(start + timedelta(days=offset))
    return result


def generate_rows(
    source: SourceData,
    max_amount: Decimal = DEFAULT_MAX_AMOUNT,
    min_amount: Decimal = DEFAULT_MIN_AMOUNT,
    seed: int = 42,
    prefer_unique: bool = True,
) -> List[AllocationRow]:
    rng = random.Random(seed)
    used_units: set[int] = set()
    rows: List[AllocationRow] = []
    uniqueness_relaxed = False

    for account in source.accounts:
        try:
            amounts = split_varied_exact_amount(
                target=account.target,
                max_amount=max_amount,
                min_amount=min_amount,
                decimals=source.decimals,
                rng=rng,
                used_units=used_units,
                prefer_unique=prefer_unique,
            )
        except ValueError as exc:
            # "Prefer unique" is a preference, not a hard failure condition.
            # If uniqueness is impossible for this account within Min/Max, retry
            # while allowing duplicates. All amount and total constraints remain.
            if prefer_unique and "varied unique amounts" in str(exc):
                uniqueness_relaxed = True
                amounts = split_varied_exact_amount(
                    target=account.target,
                    max_amount=max_amount,
                    min_amount=min_amount,
                    decimals=source.decimals,
                    rng=rng,
                    used_units=used_units,
                    prefer_unique=False,
                )
            else:
                raise

        dates = distributed_dates(source.start_date, source.end_date, len(amounts))
        for d, amount in zip(dates, amounts):
            rows.append(AllocationRow(d, account, amount))

    rows.sort(key=lambda r: (r.allocation_date, r.account.order))

    max_units = to_units(max_amount, source.decimals)
    if any(to_units(r.amount, source.decimals) > max_units for r in rows):
        raise RuntimeError("Internal error: a generated amount exceeds Maximum amount.")

    if sum(to_units(r.amount, source.decimals) for r in rows) != to_units(
        source.total_amount, source.decimals
    ):
        raise RuntimeError("Internal error: generated total does not match Total Amount.")

    for account in source.accounts:
        generated = sum(
            to_units(r.amount, source.decimals)
            for r in rows
            if r.account.order == account.order
        )
        if generated != to_units(account.target, source.decimals):
            raise RuntimeError(
                f"Internal error: generated total for {account.name} does not match target."
            )

    if prefer_unique and not uniqueness_relaxed:
        values = [to_units(r.amount, source.decimals) for r in rows]
        if len(values) != len(set(values)):
            # Safety fallback: uniqueness should normally hold here, but do not
            # abort a valid allocation solely because a duplicate remained.
            pass

    return rows


def _money_text(value: Decimal, decimals: int) -> str:
    return f"{value:.{decimals}f}" if decimals else str(int(value))


def write_output_csv(
    path: Path,
    source: SourceData,
    rows: Sequence[AllocationRow],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Acct Code", "", "Account Name", "Narration", "", "Amount"])
        for row in rows:
            writer.writerow(
                [
                    row.allocation_date.strftime("%d-%b-%y"),
                    row.account.code,
                    "",
                    row.account.name,
                    row.account.narration,
                    "",
                    _money_text(row.amount, source.decimals),
                ]
            )


# Minimal dependency-free XLSX writer.
def _xml_text(value: object) -> str:
    return escape(str(value), {'"': "&quot;", "'": "&apos;"})


def _inline_cell(ref: str, value: object, style: int = 0) -> str:
    return (
        f'<c r="{ref}" s="{style}" t="inlineStr">'
        f'<is><t xml:space="preserve">{_xml_text(value)}</t></is></c>'
    )


def _number_cell(ref: str, value: Decimal, style: int = 0) -> str:
    return f'<c r="{ref}" s="{style}"><v>{format(value, "f")}</v></c>'


def write_output_excel(
    path: Path,
    source: SourceData,
    rows: Sequence[AllocationRow],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    amount_style = 4 if source.decimals == 0 else 5

    sheet_rows: List[str] = []
    headers = ["Date", "Acct Code", "", "Account Name", "Narration", "", "Amount"]
    header_cells = [
        _inline_cell(f"{chr(65+i)}1", value, 1)
        for i, value in enumerate(headers)
    ]
    sheet_rows.append(
        '<row r="1" ht="20" customHeight="1">'
        + "".join(header_cells)
        + "</row>"
    )

    for excel_row, row in enumerate(rows, start=2):
        cells = [
            _inline_cell(f"A{excel_row}", row.allocation_date.strftime("%d-%b-%y")),
            _inline_cell(f"B{excel_row}", row.account.code),
            _inline_cell(f"C{excel_row}", ""),
            _inline_cell(f"D{excel_row}", row.account.name),
            _inline_cell(f"E{excel_row}", row.account.narration, 2),
            _inline_cell(f"F{excel_row}", ""),
            _number_cell(f"G{excel_row}", row.amount, amount_style),
        ]
        sheet_rows.append(f'<row r="{excel_row}">' + "".join(cells) + "</row>")

    last_row = max(1, len(rows) + 1)

    sheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:G{last_row}"/>
  <sheetViews>
    <sheetView workbookViewId="0">
      <pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>
    </sheetView>
  </sheetViews>
  <sheetFormatPr defaultRowHeight="15"/>
  <cols>
    <col min="1" max="1" width="13" customWidth="1"/>
    <col min="2" max="2" width="15" customWidth="1"/>
    <col min="3" max="3" width="3" customWidth="1"/>
    <col min="4" max="4" width="28" customWidth="1"/>
    <col min="5" max="5" width="39" customWidth="1"/>
    <col min="6" max="6" width="8" customWidth="1"/>
    <col min="7" max="7" width="14" customWidth="1"/>
  </cols>
  <sheetData>{''.join(sheet_rows)}</sheetData>
</worksheet>'''

    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="2">
    <numFmt numFmtId="164" formatCode="#,#0"/>
    <numFmt numFmtId="165" formatCode="#,#0.00"/>
  </numFmts>
  <fonts count="2">
    <font><sz val="10"/><name val="Calibri"/><family val="2"/></font>
    <font><b/><sz val="10"/><name val="Calibri"/><family val="2"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFA6A6A6"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFFF00"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="6">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right"/></xf>
    <xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''

    workbook_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <bookViews><workbookView xWindow="0" yWindow="0" windowWidth="18000" windowHeight="9000"/></bookViews>
  <sheets><sheet name="Allocation Schedule" sheetId="1" r:id="rId1"/></sheets>
</workbook>'''

    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>'''

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook_xml)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        z.writestr("xl/styles.xml", styles_xml)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def default_filename(source: SourceData, output_format: str) -> str:
    period = f"{source.start_date.strftime('%Y%m%d')}_{source.end_date.strftime('%Y%m%d')}"
    ext = ".xlsx" if output_format == "excel" else ".csv"
    return f"allocation_schedule_{period}{ext}"


def save_generated_file(
    output_path: Path,
    output_format: str,
    source: SourceData,
    rows: Sequence[AllocationRow],
) -> Path:
    if output_format == "excel":
        final = output_path if output_path.suffix.lower() == ".xlsx" else output_path.with_suffix(".xlsx")
        write_output_excel(final, source, rows)
        return final

    if output_format == "csv":
        final = output_path if output_path.suffix.lower() == ".csv" else output_path.with_suffix(".csv")
        write_output_csv(final, source, rows)
        return final

    raise ValueError("Output format must be Excel or CSV.")


