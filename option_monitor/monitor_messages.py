from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable
from urllib.parse import urlparse

from option_monitor.dingtalk_alert import build_markdown_payload
from option_monitor.models import (
    AnomalyChartReport,
    ContractOiChange,
    HourlyReport,
    OptionAnomaly,
    Trigger,
)
from option_monitor.settings import SHANGHAI


DEFAULT_CHART_PUBLIC_HOST = (
    "option-monitor-images.oss-cn-guangzhou.aliyuncs.com"
)
DEFAULT_CHART_PREFIX = "option-monitor/charts"


def build_alert_markdown(
    run_at_ms: int,
    triggers: Iterable[Trigger],
    coverage_text: str,
    missing_products: Iterable[str] = (),
) -> str:
    lines = [
        "## 期权监控 即时预警",
        f"北京时间：{_beijing_time(run_at_ms)}",
        f"数据覆盖：{_escape(coverage_text)}",
    ]
    missing_line = _missing_line(missing_products)
    if missing_line:
        lines.append(missing_line)

    for trigger in triggers:
        if trigger.category != "flow":
            lines.append(_format_trigger(trigger))
    return "\n\n".join(lines)


def build_option_anomaly_markdown(
    run_at_ms: int,
    anomalies: Iterable[OptionAnomaly],
    coverage_text: str,
    missing_products: Iterable[str] = (),
    other_triggers: Iterable[Trigger] = (),
) -> str:
    ordered = sorted(
        anomalies,
        key=lambda item: (
            0 if item.severity == "important" else 1,
            item.product_code,
        ),
    )
    lines = [
        "## 期权监控 异常小简报",
        f"北京时间：{_beijing_time(run_at_ms)}",
        f"数据覆盖：{_escape(coverage_text)}",
    ]
    missing_line = _missing_line(missing_products)
    if missing_line:
        lines.append(missing_line)
    for item in ordered:
        lines.append(_format_option_anomaly(item))
    remaining = tuple(
        trigger for trigger in other_triggers if trigger.category != "flow"
    )
    if remaining:
        lines.append("### 其他即时预警")
        lines.extend(_format_trigger(trigger) for trigger in remaining)
    return "\n\n".join(lines)


def build_anomaly_chart_markdown(
    report: AnomalyChartReport,
    image_url: str | None,
    chart_failed: bool,
    openvlab_image_url: str | None = None,
    openvlab_failed: bool | None = None,
    interpretation_markdown: str | None = None,
    image_public_host: str = DEFAULT_CHART_PUBLIC_HOST,
    image_prefix: str = DEFAULT_CHART_PREFIX,
) -> str:
    lines = [
        "## 期权监控 异常长图",
        f"北京时间：{_beijing_time(report.run_at_ms)}",
        f"数据覆盖：{report.collected_count}/{report.expected_count}",
    ]
    if not chart_failed and _is_safe_chart_url(
        image_url, image_public_host, image_prefix
    ):
        lines.append(f"![期权异常监控]({image_url})")
        _append_openvlab_delivery(
            lines,
            openvlab_image_url,
            openvlab_failed,
            image_public_host,
            image_prefix,
        )
    else:
        if not chart_failed:
            raise ValueError("anomaly chart delivery state is invalid")

        lines.append("图表生成失败，以下为文字降级报告")
        lines.extend([
            "### 增仓额 Top 5（资金流近似值）",
            _format_capital_flow_ranking(
                report.top_capital_increases, "本轮无有效增仓资金流"
            ),
            "### 减仓额 Top 5（资金流近似值）",
            _format_capital_flow_ranking(
                report.top_capital_decreases, "本轮无有效减仓资金流"
            ),
            "### 异常品种",
        ])
        category_names = {
            "price": "价格",
            "iv": "ATM IV",
            "oi": "持仓",
            "skew": "偏度",
        }
        for card in report.cards:
            level = {
                "important": "重要",
                "warning": "预警",
                "observation": "观察",
            }[card.severity]
            categories = " / ".join(
                category_names[item] for item in card.trigger_categories
            )
            details = []
            if (
                card.price_triggered
                and card.futures_change_percent is not None
            ):
                details.append(
                    f"价格 {card.futures_change_percent * Decimal('100'):+.2f}%"
                )
            if card.atm_iv.triggered and card.atm_iv.change is not None:
                details.append(
                    f"ΔIV {card.atm_iv.change * Decimal('100'):+.2f} pp"
                )
            if "oi" in card.trigger_categories:
                call_delta = (
                    "基线不足" if card.call_oi_delta is None
                    else f"{card.call_oi_delta:+d}张"
                )
                put_delta = (
                    "基线不足" if card.put_oi_delta is None
                    else f"{card.put_oi_delta:+d}张"
                )
                details.append(f"Call OI {call_delta} / Put OI {put_delta}")
            if card.rr25.triggered and card.rr25.change is not None:
                details.append(
                    f"ΔRR25 {card.rr25.change * Decimal('100'):+.2f} pp"
                )
            if card.oi_pcr is not None:
                previous = (
                    "--" if card.previous_oi_pcr is None
                    else f"{card.previous_oi_pcr:.2f}"
                )
                change = (
                    "--" if card.oi_pcr_change is None
                    else f"{card.oi_pcr_change * Decimal('100'):+.2f}%"
                )
                details.append(
                    f"OI PCR {card.oi_pcr:.2f}（昨 {previous}，{change}）"
                )
            detail_text = f" | {'；'.join(details)}" if details else ""
            lines.append(
                f"- {level} {card.strength_score}/100 | "
                f"{card.direction_label} | "
                f"{_product_label(card.product_name, card.product_code, card.underlying)} | "
                f"{categories}{detail_text}"
            )
        _append_openvlab_delivery(
            lines,
            openvlab_image_url,
            openvlab_failed,
            image_public_host,
            image_prefix,
        )
    if interpretation_markdown:
        lines.append(interpretation_markdown)
    return "\n\n".join(lines)


def _append_openvlab_delivery(
    lines: list[str],
    image_url: str | None,
    failed: bool | None,
    image_public_host: str,
    image_prefix: str,
) -> None:
    if failed is None:
        if image_url is not None:
            raise ValueError(
                "OpenVLab snapshot delivery state is invalid"
            )
        return
    if failed:
        if image_url is not None:
            raise ValueError(
                "OpenVLab snapshot delivery state is invalid"
            )
        lines.append("OpenVLab 排名快照获取失败")
        return
    if not _is_safe_chart_url(
        image_url, image_public_host, image_prefix
    ):
        raise ValueError("OpenVLab snapshot delivery state is invalid")
    lines.extend([
        "### OpenVLab 期权增减仓额 Top 8",
        f"![OpenVLab 期权增减仓额 Top 8]({image_url})",
    ])


def _format_oi_ranking(
    rows: Iterable[ContractOiChange], empty_text: str
) -> str:
    formatted = [
        f"{index}. {_escape(row.symbol)} [{row.side}] "
        f"{row.open_interest:,} / {row.pre_open_interest:,} / "
        f"{row.delta_open_interest:+,} 张"
        for index, row in enumerate(rows, start=1)
    ]
    return "\n".join(formatted) if formatted else empty_text


def _format_capital_flow_ranking(
    rows: Iterable[ContractOiChange], empty_text: str
) -> str:
    formatted = [
        f"{index}. {_escape(row.symbol)} [{row.side}] "
        f"{_compact_money(abs(row.oi_capital_flow))} "
        f"({row.delta_open_interest:+,} 张)"
        for index, row in enumerate(rows, start=1)
        if row.oi_capital_flow is not None
    ]
    return "\n".join(formatted) if formatted else empty_text


def _compact_money(value: Decimal) -> str:
    if value >= Decimal("100000000"):
        return f"{value / Decimal('100000000'):,.2f} 亿"
    if value >= Decimal("10000"):
        return f"{value / Decimal('10000'):,.2f} 万"
    return f"{value:,.2f} 元"


def _format_option_anomaly(item: OptionAnomaly) -> str:
    level = "重要" if item.severity == "important" else "一般"
    product = _product_label(
        item.product_name, item.product_code, item.underlying
    )
    trigger_names = " + ".join(
        "异常 IV" if trigger == "iv" else "异常偏度"
        for trigger in item.triggers
    )
    lines = [
        f"### {level} | {product}",
        f"触发：{trigger_names}",
        _format_anomaly_thresholds(item),
        _format_anomaly_market(item),
        _format_anomaly_volatility(item),
        _format_anomaly_skew(item),
        _format_anomaly_flow(item),
        _format_anomaly_oi(item),
        _format_anomaly_term(item),
        _format_anomaly_move(item),
        _format_evidence("价格有没有配合", "price", item),
        _format_evidence("IV有没有配合", "iv", item),
        _format_evidence("OI有没有确认", "oi", item),
        f"结论：{_anomaly_conclusion(item)}",
    ]
    if item.pin_risk:
        lines.append("到期提示：OI 高度集中且临近到期，注意支撑、压力或 Pin 风险。")
    return "\n\n".join(lines)


def _format_anomaly_thresholds(item: OptionAnomaly) -> str:
    values = []
    if "iv" in item.triggers:
        rank = str(item.iv_rank) if item.iv_rank is not None else "数据不足"
        mean = (
            _points(item.iv_mean, signed=False)
            if item.iv_mean is not None else "数据不足"
        )
        values.append(f"IV 十日排名 {rank}，十日均值 {mean}")
    if "skew" in item.triggers:
        rank = str(item.skew_rank) if item.skew_rank is not None else "数据不足"
        mean = (
            _points(item.mean_abs_skew_change, signed=False)
            if item.mean_abs_skew_change is not None else "数据不足"
        )
        values.append(f"|ΔRR25| 十日排名 {rank}，十日均值 {mean}")
    return "阈值：" + "；".join(values)


def _format_anomaly_market(item: OptionAnomaly) -> str:
    change = (
        _percent(item.price_change)
        if item.price_change is not None else "数据不足"
    )
    return f"价格：期货日内涨跌 {change}"


def _format_anomaly_volatility(item: OptionAnomaly) -> str:
    delta = _points(item.delta_iv) if item.delta_iv is not None else "数据不足"
    hv = _points(item.hv10, signed=False) if item.hv10 is not None else "数据不足"
    spread = _points(item.iv_hv) if item.iv_hv is not None else "数据不足"
    return (
        f"波动率：ATM IV {_points(item.atm_iv, signed=False)}，"
        f"ΔIV {delta}，HV10 {hv}，IV-HV {spread}"
    )


def _format_anomaly_skew(item: OptionAnomaly) -> str:
    rr = _points(item.rr25) if item.rr25 is not None else "数据不足"
    delta = (
        _points(item.delta_rr25)
        if item.delta_rr25 is not None else "数据不足"
    )
    direction = {
        "call": "Call 翼相对走贵",
        "put": "Put 翼相对走贵",
        "neutral": "方向中性",
    }[item.side]
    return f"偏度：RR25 {rr}，ΔRR25 {delta}，{direction}"


def _format_anomaly_flow(item: OptionAnomaly) -> str:
    option = item.option
    if not option.flow_baseline_ready:
        return "成交偏向：数据不足（十分钟基线未建立）"
    side = {"call": "Call", "put": "Put", "neutral": "中性"}[
        _flow_side(option)
    ]
    volume_pcr = _ratio_text(option.volume_pcr)
    turnover_pcr = _ratio_text(option.turnover_pcr)
    return (
        f"成交偏向：{side}；十分钟 Call {option.call_volume_delta:,} 手 / "
        f"{_money(option.call_turnover_delta)}，Put {option.put_volume_delta:,} 手 / "
        f"{_money(option.put_turnover_delta)}；Volume PCR {volume_pcr}，"
        f"Turnover PCR {turnover_pcr}"
    )


def _format_anomaly_oi(item: OptionAnomaly) -> str:
    option = item.option
    if not option.oi_baseline_ready:
        delta_text = "数据不足"
    else:
        delta_text = (
            f"Call {option.call_open_interest - option.call_pre_open_interest:+,}，"
            f"Put {option.put_open_interest - option.put_pre_open_interest:+,}"
        )
    concentrations = "，".join(
        f"{concentration.strike} ({concentration.share * Decimal('100'):.1f}%)"
        for concentration in option.oi_concentrations
    ) or "数据不足"
    return (
        f"OI：PCR {_ratio_text(option.oi_pcr)}；较昨变化 {delta_text}；"
        f"集中行权价 {concentrations}"
    )


def _format_anomaly_term(item: OptionAnomaly) -> str:
    option = item.option
    if option.next_expire is None or option.next_atm_iv is None:
        return "期限结构：数据不足"
    slope = option.next_atm_iv - item.atm_iv
    return (
        f"期限结构：近月 {option.expire} {_points(item.atm_iv, signed=False)}，"
        f"次月 {option.next_expire} {_points(option.next_atm_iv, signed=False)}，"
        f"次月-近月 {_points(slope)}"
    )


def _format_anomaly_move(item: OptionAnomaly) -> str:
    if item.implied_move_pct is None or item.implied_move_amount is None:
        return "隐含波动幅度：数据不足"
    return (
        f"隐含波动幅度：到期约 ±{_percent(item.implied_move_pct, signed=False)}，"
        f"约 ±{item.implied_move_amount:,.2f} 点"
    )


def _format_evidence(label: str, key: str, item: OptionAnomaly) -> str:
    return f"{label}：{'是' if key in item.evidence else '未确认'}"


def _anomaly_conclusion(item: OptionAnomaly) -> str:
    price = "price" in item.evidence
    iv = "iv" in item.evidence
    if item.side == "call":
        if price and iv:
            return "Call 侧信号、价格与 IV 配合，强势信号得到确认。"
        if not price:
            return "Call 侧信号但价格未配合，可能存在卖压，谨慎。"
    if item.side == "put":
        if price and iv:
            return "Put 侧信号、价格与 IV 配合，弱势信号得到确认。"
        if not price:
            return "Put 侧信号但价格未配合，可能是保护盘，不直接判空。"
    if iv and not price:
        return "IV 上升而标的未配合，注意潜在事件风险。"
    return "当前证据未形成一致方向，继续观察价格、IV 与 OI。"


def _ratio_text(value: Decimal | None) -> str:
    return "数据不足" if value is None else f"{value:.2f}"


def _flow_side(option: object) -> str:
    if option.call_turnover_delta > Decimal("0") and option.put_turnover_delta == Decimal("0"):
        return "call"
    if option.put_turnover_delta > Decimal("0") and option.call_turnover_delta == Decimal("0"):
        return "put"
    if option.turnover_pcr is None:
        return "neutral"
    if option.turnover_pcr <= Decimal("0.8"):
        return "call"
    if option.turnover_pcr >= Decimal("1.25"):
        return "put"
    return "neutral"


def _money(value: Decimal) -> str:
    return f"{value:,.2f} 元"


def build_hourly_markdown(
    report: HourlyReport,
    image_url: str | None = None,
    image_upload_failed: bool = False,
) -> str:
    lines = [
        "## 期权监控 小时报告",
        f"北京时间：{_beijing_time(report.run_at_ms)}",
        f"覆盖率：{_percent(report.coverage_ratio, signed=False)}",
    ]
    missing_line = _missing_line(report.missing_products)
    if missing_line:
        lines.append(missing_line)
    missing_close_line = _product_list_line(
        "缺少昨收基线", report.missing_close_products
    )
    if missing_close_line:
        lines.append(missing_close_line)
    missing_price_line = _product_list_line(
        "缺少期货日内涨跌幅", report.missing_price_products
    )
    if missing_price_line:
        lines.append(missing_price_line)
    lines.extend([
        "### 期货日内涨跌幅 Top5",
        _format_price_entries(report.price_entries),
        "### 较昨收 |ΔIV| Top5",
        _format_iv_entries(report.iv_entries),
    ])
    if _is_safe_chart_url(image_url):
        lines.extend([
            "### IV 图表",
            f"![IV 波动率监控]({image_url})",
        ])
    elif image_upload_failed:
        lines.append("图表上传失败")
    return "\n\n".join(lines)


def build_service_markdown(
    run_at_ms: int,
    coverage_ratio: Decimal,
    missing_products: Iterable[str],
) -> str:
    lines = [
        "## 期权监控 服务状态",
        f"北京时间：{_beijing_time(run_at_ms)}",
        f"覆盖率：{_percent(coverage_ratio, signed=False)}",
    ]
    missing_line = _missing_line(missing_products)
    if missing_line:
        lines.append(missing_line)
    return "\n\n".join(lines)


def _format_trigger(trigger: Trigger) -> str:
    label = "重要" if trigger.severity == "important" else "一般"
    product = _product_label(
        trigger.product_name,
        trigger.product_code,
        str(trigger.details.get("underlying", trigger.product_code)),
    )
    direction = _escape(trigger.direction)
    if trigger.category == "price":
        return f"- {label} | {product} | {direction} {_percent(trigger.value)}"
    if trigger.category == "iv":
        return f"- {label} | {product} | ATM IV {_points(trigger.value)}"
    return f"- {label} | {product} | {direction}"


def _format_price_entries(entries: Iterable[dict[str, object]]) -> str:
    formatted = [
        f"{index}. {_product_label(str(row['product_name']), str(row['product_code']), str(row.get('underlying', row['product_code'])))} "
        f"{'上涨' if Decimal(row['price_change']) >= 0 else '下跌'} {_percent(Decimal(row['price_change']))}"
        for index, row in enumerate(entries, start=1)
    ]
    return "\n".join(formatted) if formatted else "无数据"
def _format_iv_entries(entries: Iterable[dict[str, object]]) -> str:
    formatted = [
        f"{index}. {_product_label(str(row['product_name']), str(row['product_code']), str(row.get('underlying', row['product_code'])))} "
        f"ATM IV {_points(Decimal(row['atm_iv']), signed=False)}，"
        f"ΔIV {_points(Decimal(row['delta_iv']))}"
        for index, row in enumerate(entries, start=1)
    ]
    return "\n".join(formatted) if formatted else "无数据"


def _is_safe_chart_url(
    url: str | None,
    public_host: str = DEFAULT_CHART_PUBLIC_HOST,
    prefix: str = DEFAULT_CHART_PREFIX,
) -> bool:
    if (
        not isinstance(url, str)
        or not url
        or any(
            character.isspace()
            or character in "()[]<>\\"
            or ord(character) == 127
            for character in url
        )
    ):
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == public_host
        and port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path.startswith(f"/{prefix}/")
        and parsed.path.endswith(".png")
    )


def _beijing_time(run_at_ms: int) -> str:
    return datetime.fromtimestamp(run_at_ms / 1000, tz=SHANGHAI).strftime("%Y-%m-%d %H:%M")


def _percent(value: Decimal, signed: bool = True) -> str:
    rendered = format(value * Decimal("100"), "+,.2f" if signed else ",.2f")
    return f"{rendered}%"


def _points(value: Decimal, signed: bool = True) -> str:
    rendered = format(value * Decimal("100"), "+,.2f" if signed else ",.2f")
    return f"{rendered}百分点"
def _product_label(name: str, code: str, underlying: str) -> str:
    return (
        f"{_escape(name)} "
        f"({_escape(code)} / {_escape(underlying)})"
    )


def _missing_line(missing_products: Iterable[str]) -> str:
    return _product_list_line("缺失品种", missing_products)


def _product_list_line(label: str, products: Iterable[str]) -> str:
    escaped = tuple(_escape(product) for product in products)
    return f"{label}：{', '.join(escaped)}" if escaped else ""


def _escape(value: str) -> str:
    single_line = " ".join(str(value).splitlines())
    markdown_characters = frozenset("\\*_~`#+-.![]()<>|{}")
    return "".join(
        f"\\{character}" if character in markdown_characters else character
        for character in single_line
    )
