"""
One-shot: fill the hand-written Bucket G ground truth (stage 4 for the
visualize path) into questions_visualize.json.

Mapping rules (grounded in pipeline/clickhouse/init.sql + tools/visualize.py):
  • visualize tool reads ONE OLAP table, no joins → expected_chart picks
    the single table that covers the most of the question's metrics.
  • chart_type holds the *tool name(s)* the grader compares against
    (`line`/`bar`/`pie`), since metrics/agent.py checks call.tool ∈ list.
  • Metrics that don't exist in the generic OLAP schema (NIM, CASA, NPL,
    credit growth, segment revenue mix) are NOT chartable — the expected
    chart covers what IS chartable; the gap is documented in
    expected_data_facts so a reviewer/caption can flag the limitation.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "questions_visualize.json"

TREND_CAP = [
    "đề cập chuỗi 5 năm 2020-2024",
    "nêu đơn vị (tỷ đồng cho giá trị, % cho biên/ROE)",
    "có nhận xét xu hướng tăng/giảm",
]
PEER_CAP = [
    "đề cập năm 2024 và các mã được so sánh",
    "nêu đơn vị của từng chỉ tiêu",
    "có nhận xét mã nào dẫn đầu / thấp nhất",
]

# id -> (chart_type, table, x_column, y_columns, filters, order_by,
#        n_points, data_facts, extra_caption)
G = {
 "G-001": (["line","bar"],"income_statement","year",["revenue","net_profit"],
   {"symbol":"VNM","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["VNM doanh thu ~60-61 ngàn tỷ năm 2024","LNST ~ 8.5-9.5 ngàn tỷ/năm",
    "có data đủ cho từng năm 2020-2024","doanh thu đi ngang/tăng nhẹ giai đoạn này"],
   ["biên LN ròng (~15%) là % nên tách trục/biểu đồ riêng khỏi giá trị tỷ đồng"]),
 "G-002": (["line","bar"],"income_statement","year",["revenue","net_profit"],
   {"symbol":"FPT","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["FPT doanh thu tăng đều ~30k → ~62k tỷ 2020-2024","LNST tăng đều mỗi năm",
    "ROE ~25-28% (ở financial_ratios)","data đủ cho từng năm"],
   ["ROE là % ở bảng financial_ratios — chart riêng khỏi doanh thu/LNST"]),
 "G-003": (["line"],"income_statement","year",["revenue","net_profit"],
   {"symbol":"HPG","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["HPG mang tính chu kỳ: LNST đỉnh 2021 (~34k tỷ), giảm sâu 2022-2023, hồi 2024",
    "doanh thu thuần dao động theo chu kỳ thép","data đủ từng năm 2020-2024"],
   ["biên gộp (%) ở financial_ratios.gross_margin — tách khỏi giá trị tuyệt đối"]),
 "G-004": (["bar"],"income_statement","year",["revenue","net_profit","operating_expenses"],
   {"symbol":"MWG","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["MWG doanh thu tăng tới ~133k tỷ (2022) rồi chững 2023","LN ròng giảm mạnh 2023",
    "operating_expenses xấp xỉ chi phí bán hàng (OLAP không tách riêng selling expense)"],
   ["dùng biểu đồ cột theo năm như yêu cầu"]),
 "G-005": (["line","bar"],"financial_ratios","year",["roe"],
   {"symbol":"VCB","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["VCB ROE ~20-22%, cao nhất nhóm NHTM quốc doanh","data ROE đủ từng năm",
    "NIM và tỷ lệ nợ xấu KHÔNG có trong schema OLAP → không chart được"],
   ["caption nên nêu rõ chỉ chart được ROE; NIM/nợ xấu ngoài phạm vi dữ liệu"]),
 "G-006": (["line","bar"],"income_statement","year",["profit_before_tax"],
   {"symbol":"BID","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["BID lợi nhuận trước thuế tăng đều 2020-2024","data đủ từng năm",
    "NIM và nợ xấu không có trong OLAP → ngoài phạm vi"],
   ["caption nêu giới hạn dữ liệu cho NIM/nợ xấu"]),
 "G-007": (["line","bar"],"income_statement","year",["revenue","net_profit"],
   {"symbol":"VHM","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["VHM doanh thu biến động mạnh theo tiến độ bàn giao BĐS","LNST cao, biến động",
    "nợ vay xấp xỉ liabilities_total ở balance_sheet (bảng khác)"],
   ["tool không có 'combo' — line hoặc bar đều chấp nhận"]),
 "G-008": (["line","bar"],"income_statement","year",["revenue","net_profit"],
   {"symbol":"VIC","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["VIC doanh thu hợp nhất lớn","LNST mỏng, một số năm âm",
    "dòng tiền KD ở cash_flow_statement.net_cash_from_operating_activities (bảng khác)"],
   ["dòng tiền kinh doanh phải vẽ từ bảng cash_flow_statement riêng"]),
 "G-009": (["bar"],"income_statement","year",["revenue","net_profit"],
   {"symbol":"MSN","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["MSN doanh thu hợp nhất tăng 2020-2022 rồi đi ngang",
    "OLAP KHÔNG có breakdown doanh thu theo mảng → không vẽ được stacked theo segment",
    "chỉ chart được tổng doanh thu + LNST"],
   ["agent nên nêu hạn chế: không có dữ liệu cơ cấu doanh thu theo mảng"]),
 "G-010": (["line","bar"],"financial_ratios","year",["roe"],
   {"symbol":"TCB","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["TCB ROE cao ~18-22% giai đoạn 2020-2024","data ROE đủ từng năm",
    "CASA và NIM không có trong OLAP → ngoài phạm vi"],
   ["caption nêu chỉ chart được ROE; CASA/NIM ngoài dữ liệu"]),
 "G-011": (["line","bar"],"income_statement","year",["profit_before_tax"],
   {"symbol":"ACB","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["ACB lợi nhuận trước thuế tăng đều 2020-2024","ROE ~24% (ở financial_ratios)",
    "nợ xấu không có trong OLAP → ngoài phạm vi"],
   ["ROE ở bảng financial_ratios; nợ xấu ngoài dữ liệu"]),
 "G-012": (["line","bar"],"income_statement","year",["profit_before_tax"],
   {"symbol":"MBB","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["MBB lợi nhuận trước thuế tăng mạnh 2020-2024","data đủ từng năm",
    "tăng trưởng tín dụng, NIM, nợ xấu không có trong OLAP → ngoài phạm vi"],
   ["caption nêu chỉ chart được LNTT; các chỉ tiêu ngân hàng còn lại ngoài dữ liệu"]),
 "G-013": (["line","bar"],"income_statement","year",["revenue","net_profit"],
   {"symbol":"SAB","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["SAB doanh thu giảm 2020-2021 (COVID) rồi hồi phục","LNST cao, biên ròng ~20%+",
    "data đủ từng năm 2020-2024"],
   ["biên LN ròng (%) ở financial_ratios — tách khỏi giá trị tỷ đồng"]),
 "G-014": (["line"],"income_statement","year",["revenue","net_profit"],
   {"symbol":"GAS","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["GAS doanh thu & LNST biến động theo giá dầu/khí","2020 giảm sâu, 2022 phục hồi mạnh",
    "dòng tiền HĐKD ở cash_flow_statement (bảng khác)"],
   ["dùng line chart như yêu cầu; dòng tiền KD vẽ từ cash_flow_statement riêng"]),
 "G-015": (["line","bar"],"income_statement","year",["revenue","net_profit"],
   {"symbol":"PLX","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["PLX doanh thu rất lớn (~120-300k tỷ) nhưng biên ròng mỏng ~1-2%",
    "2020 LN thấp/lỗ do COVID + giá dầu","data đủ từng năm"],
   ["biên LN ròng (%) ở financial_ratios — tách trục riêng"]),
 "G-016": (["line","bar"],"income_statement","year",["revenue","net_profit"],
   {"symbol":"PNJ","quarter":0,"year":"2020-2024"},[["year","asc"]],5,
   ["PNJ doanh thu & LN ròng tăng trưởng đều (trừ 2021 giảm do giãn cách)",
    "hàng tồn kho ở balance_sheet.inventory (bảng khác)",
    "biên gộp ở financial_ratios.gross_margin (bảng khác)"],
   ["hàng tồn kho & biên gộp ở bảng khác — vẽ riêng nếu cần"]),
 "G-017": (["bar"],"financial_ratios","symbol",["roe","eps_vnd","price_to_earnings"],
   {"symbol":["FPT","VNM","MWG","PNJ"],"year":2024,"quarter":0},[["roe","desc"]],4,
   ["FPT ROE cao nhất nhóm (~28%)","P/E khác biệt rõ giữa 4 mã",
    "đủ data 2024 cho cả 4 mã trong financial_ratios"],
   ["ROE/EPS/P/E khác đơn vị → nên normalize hoặc tách biểu đồ"]),
 "G-018": (["bar"],"financial_ratios","symbol",["roe"],
   {"symbol":["VCB","BID","TCB","ACB","MBB"],"year":2024,"quarter":0},[["roe","desc"]],5,
   ["VCB/TCB/ACB ROE ~20%+ năm 2024","data ROE đủ cho 5 mã",
    "NIM và nợ xấu không có trong OLAP → chỉ so sánh được ROE"],
   ["caption nêu chỉ peer-compare được ROE; NIM/nợ xấu ngoài dữ liệu"]),
 "G-019": (["bar"],"income_statement","symbol",["revenue","net_profit"],
   {"symbol":["HPG","DGC","DCM","DPM"],"year":2024,"quarter":0},[["revenue","desc"]],4,
   ["HPG doanh thu lớn nhất nhóm 2024","DGC biên cao nhất",
    "biên gộp ở financial_ratios.gross_margin (bảng khác)"],
   ["biên gộp (%) ở bảng khác — so sánh riêng"]),
 "G-020": (["bar"],"income_statement","symbol",["net_profit"],
   {"symbol":["SSI","VND","HCM"],"year":2024,"quarter":0},[["net_profit","desc"]],3,
   ["SSI dẫn đầu về LNST 2024 trong nhóm CTCK","data 2024 đủ cho 3 mã",
    "ROE ở financial_ratios, vốn chủ ở balance_sheet.equity_total (bảng khác)"],
   ["ROE và vốn chủ sở hữu ở bảng khác — so sánh riêng"]),
}


def main():
    items = json.loads(SRC.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in items}
    missing = [i for i in G if i not in by_id]
    extra = [c["id"] for c in items if c["id"] not in G]
    if missing or extra:
        raise SystemExit(f"id mismatch: missing={missing} unmapped={extra}")

    for c in items:
        ct, table, xcol, ycols, filt, order, npts, dfacts, ecap = G[c["id"]]
        c["expected_chart"] = {
            "chart_type": ct,
            "table": table,
            "x_column": xcol,
            "y_columns": ycols,
            "filters": filt,
            "order_by": order,
            "expected_n_points": npts,
        }
        c["expected_data_facts"] = dfacts
        is_peer = c.get("scope") == "multi_company"
        c["expected_caption_facts"] = (PEER_CAP if is_peer else TREND_CAP) + ecap
        # Bucket G has no reference text — keep schema consistent.
        c["reference_answer"] = None
        c["sources"] = []
        c["key_facts"] = []

    SRC.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"enriched {len(items)} bucket-G cases -> {SRC.name}")


if __name__ == "__main__":
    main()
