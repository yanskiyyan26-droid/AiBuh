from __future__ import annotations
import csv
import sys
import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple
import json
import math

try:
    from dateutil.relativedelta import relativedelta
except Exception:
    # minimal fallback
    class relativedelta:
        def __init__(self, months=0):
            self.months = months
        def __radd__(self, other):
            m = other.month + self.months
            y = other.year + (m-1)//12
            m = (m-1)%12 + 1
            return date(y,m,other.day)

# Optional libs
HAS_PANDAS = False
HAS_OPENPYXL = False
HAS_FASTAPI = False
try:
    import pandas as pd
    HAS_PANDAS = True
except Exception:
    pass
try:
    import openpyxl
    HAS_OPENPYXL = True
except Exception:
    pass
try:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, FileResponse
    import uvicorn
    HAS_FASTAPI = True
except Exception:
    pass

# -------------------- Конфигурация (параметры 2025, изменить при необходимости) --------------------
MRP = 3932
VAT_THRESHOLD_MRP = 20_000
VAT_THRESHOLD_KZT = VAT_THRESHOLD_MRP * MRP
VAT_RATE = 0.12
CIT_RATE = 0.20
PIT_RATE_IP_GENERAL = 0.10
OPV_RATE = 0.10
OPVR_RATE = 0.025
OOSMS_EMPLOYER = 0.03
OOSMS_EMPLOYEE = 0.02
SOCIAL_CONTRIBUTION_RATE = 0.05

# Payment schedule rules (deadlines examples) - customizable per entity
DEADLINES = {
    "VAT_monthly": {"day": 25, "period": "monthly"},
    "IncomeTax_annual": {"day": 31, "month": 12, "period": "annual"},
    "Social_monthly": {"day": 25, "period": "monthly"}
}

# -------------------- Модель данных --------------------
@dataclass
class Operation:
    id: int
    date: date
    kind: str  # 'income' or 'expense'
    amount: float
    description: str = ""
    taxable_vat: bool = True
    vat_included: bool = False  # если True, amount содержит НДС
    counterparty: Optional[str] = None

@dataclass
class Employee:
    id: int
    name: str
    gross_salary: float
    start_date: Optional[date] = None
    end_date: Optional[date] = None

@dataclass
class AccountBook:
    company_type: str  # 'ip' or 'company'
    regime: str = "general"  # 'general', 'patent', 'simplified'
    ops: List[Operation] = field(default_factory=list)
    employees: List[Employee] = field(default_factory=list)
    next_op_id: int = 1

    def add_operation(self, op: Operation):
        op.id = self.next_op_id
        self.next_op_id += 1
        self.ops.append(op)

    def add_income(self, amount: float, date_: Optional[date]=None, description: str="", taxable_vat: bool=True, vat_included=False, counterparty: Optional[str]=None):
        d = date_ or date.today()
        self.add_operation(Operation(0, d, 'income', amount, description, taxable_vat, vat_included, counterparty))

    def add_expense(self, amount: float, date_: Optional[date]=None, description: str="", taxable_vat: bool=True, vat_included=False, counterparty: Optional[str]=None):
        d = date_ or date.today()
        self.add_operation(Operation(0, d, 'expense', amount, description, taxable_vat, vat_included, counterparty))

    def add_employee(self, emp: Employee):
        self.employees.append(emp)

    # Basic totals
    def total_income(self, start: Optional[date]=None, end: Optional[date]=None) -> float:
        return sum(o.amount for o in self._filter_ops(start, end) if o.kind == 'income')

    def total_expenses(self, start: Optional[date]=None, end: Optional[date]=None) -> float:
        return sum(o.amount for o in self._filter_ops(start, end) if o.kind == 'expense')

    def profit(self, start: Optional[date]=None, end: Optional[date]=None) -> float:
        return self.total_income(start, end) - self.total_expenses(start, end)

    def turnover(self, months: int = 12, up_to: Optional[date]=None) -> float:
        up_to = up_to or date.today()
        start = self._months_ago(up_to, months)
        return self.total_income(start, up_to)

    def is_vat_required(self, months: int = 12) -> bool:
        return self.turnover(months) >= VAT_THRESHOLD_KZT

    def vat_report(self, start: Optional[date]=None, end: Optional[date]=None) -> Dict[str, float]:
        # counts output VAT and input VAT (simplified)
        ops = self._filter_ops(start, end)
        output_taxable = sum(o.amount for o in ops if o.kind == 'income' and o.taxable_vat)
        input_taxable = sum(o.amount for o in ops if o.kind == 'expense' and o.taxable_vat)
        # Handle vat_included amounts: if amount includes VAT, separate base
        output_vat = 0.0
        input_vat = 0.0
        for o in ops:
            if o.kind == 'income' and o.taxable_vat:
                if o.vat_included:
                    base = o.amount / (1 + VAT_RATE)
                    output_vat += o.amount - base
                    output_taxable += base - o.amount if False else 0  # keep taxable base separate if needed
                else:
                    output_vat += o.amount * VAT_RATE
            if o.kind == 'expense' and o.taxable_vat:
                if o.vat_included:
                    base = o.amount / (1 + VAT_RATE)
                    input_vat += o.amount - base
                else:
                    input_vat += o.amount * VAT_RATE
        net_vat = output_vat - input_vat
        return {"output_vat": output_vat, "input_vat": input_vat, "net_vat": net_vat}

    def pit_due(self, start: Optional[date]=None, end: Optional[date]=None) -> float:
        if self.company_type != 'ip':
            return 0.0
        if self.regime != 'general':
            return 0.0
        p = self.profit(start, end)
        return max(0.0, p * PIT_RATE_IP_GENERAL)

    def cit_due(self, start: Optional[date]=None, end: Optional[date]=None) -> float:
        if self.company_type != 'company':
            return 0.0
        p = self.profit(start, end)
        return max(0.0, p * CIT_RATE)

    def social_payments(self, start: Optional[date]=None, end: Optional[date]=None, declared_income: Optional[float]=None) -> Dict[str, float]:
        base = declared_income if declared_income is not None else self.total_income(start, end)
        opv = base * OPV_RATE
        vosms = base * OOSMS_EMPLOYEE
        so = base * SOCIAL_CONTRIBUTION_RATE
        return {"base": base, "opv": opv, "vosms": vosms, "so": so, "total": opv + vosms + so}

    def payroll_summary(self, month: date) -> Dict[str, Any]:
        # payroll calculations for all employees in specified month
        total_gross = sum(e.gross_salary for e in self.employees if (not e.start_date or e.start_date <= month) and (not e.end_date or e.end_date >= month))
        employee_withholdings = sum(e.gross_salary * OOSMS_EMPLOYEE + e.gross_salary * 0.10 for e in self.employees)
        # here 0.10 - placeholder for personal income tax on salary; real rules differ (PIT/kazakh payroll rules)
        employer_costs = total_gross + total_gross * (OPVR_RATE + OOSMS_EMPLOYER + SOCIAL_CONTRIBUTION_RATE)
        return {"month": month, "total_gross": total_gross, "employee_withholdings": employee_withholdings, "employer_costs": employer_costs}

    # Recurring operations generator
    def generate_recurring(self, template: Operation, start: date, end: date, freq_months: int = 1) -> List[Operation]:
        out = []
        cur = start
        while cur <= end:
            op = Operation(0, cur, template.kind, template.amount, template.description, template.taxable_vat, template.vat_included, template.counterparty)
            self.add_operation(op)
            out.append(op)
            # advance months
            cur = self._months_ago(cur, -freq_months)  # add months
        return out

    # Reporting and export
    def summary_report(self, start: Optional[date]=None, end: Optional[date]=None) -> Dict[str, Any]:
        start = start or date(self._earliest_year(),1,1)
        end = end or date.today()
        return {
            "company_type": self.company_type,
            "regime": self.regime,
            "period": (start.isoformat(), end.isoformat()),
            "income": self.total_income(start, end),
            "expenses": self.total_expenses(start, end),
            "profit": self.profit(start, end),
            "vat": self.vat_report(start, end),
            "pit_due": self.pit_due(start, end),
            "cit_due": self.cit_due(start, end),
            "social": self.social_payments(start, end)
        }

    def export_csv(self, filename: str):
        with open(filename, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['id','date','kind','amount','description','taxable_vat','vat_included','counterparty'])
            for o in self.ops:
                w.writerow([o.id, o.date.isoformat(), o.kind, o.amount, o.description, o.taxable_vat, o.vat_included, o.counterparty])

    def export_excel(self, filename: str):
        if not HAS_PANDAS:
            raise RuntimeError('pandas required for excel export')
        df = pd.DataFrame([{
            'id': o.id,
            'date': o.date,
            'kind': o.kind,
            'amount': o.amount,
            'description': o.description,
            'taxable_vat': o.taxable_vat,
            'vat_included': o.vat_included,
            'counterparty': o.counterparty
        } for o in self.ops])
        df.to_excel(filename, index=False)

    # Deadlines generator (creates upcoming deadlines for next n months)
    def upcoming_deadlines(self, months_ahead: int = 3) -> List[Tuple[date, str]]:
        out = []
        today = date.today()
        for n in range(months_ahead):
            dt = self._months_ago(today, -n)
            # VAT monthly
            d_vat = date(dt.year, dt.month, min(DEADLINES['VAT_monthly']['day'], 28))
            out.append((d_vat, 'VAT monthly due'))
            # Social
            d_soc = date(dt.year, dt.month, min(DEADLINES['Social_monthly']['day'],28))
            out.append((d_soc, 'Social payments due'))
        # annual
        year_end = date(today.year, DEADLINES['IncomeTax_annual']['month'], DEADLINES['IncomeTax_annual']['day'])
        out.append((year_end, 'Annual income tax declaration due'))
        return sorted(out)

    # Helpers
    def _filter_ops(self, start: Optional[date]=None, end: Optional[date]=None) -> List[Operation]:
        start = start or date.min
        end = end or date.max
        return [o for o in self.ops if start <= o.date <= end]

    def _earliest_year(self) -> int:
        if not self.ops:
            return date.today().year
        return min(o.date for o in self.ops).year

    def _months_ago(self, up_to: date, months: int) -> date:
        # months may be negative to go forward
        y = up_to.year + (up_to.month - 1 + months) // 12
        m = (up_to.month - 1 + months) % 12 + 1
        d = min(up_to.day, 28)
        return date(y,m,d)

# -------------------- Пример и CLI --------------------

SAMPLE_DATA_FILE = 'ai_buh_sample.json'

def create_sample_book() -> AccountBook:
    b = AccountBook(company_type='ip', regime='general')
    b.add_income(120_000, date(2025,1,5), 'Продажа товаров', taxable_vat=True, vat_included=False, counterparty='ООО Клиент')
    b.add_expense(25_000, date(2025,1,7), 'Закупка материалов', taxable_vat=True, vat_included=False, counterparty='Поставщик')
    b.add_expense(8_000, date(2025,1,10), 'Аренда офиса', taxable_vat=False, vat_included=False)
    b.add_expense(5_000, date(2025,1,15), 'Интернет и связь', taxable_vat=False, vat_included=False)
    b.add_employee(Employee(1, 'Иванов И.И.', 200_000, date(2024,5,1)))
    return b

# persistence

def save_book(book: AccountBook, filename: str = SAMPLE_DATA_FILE):
    data = {
        'company_type': book.company_type,
        'regime': book.regime,
        'ops': [{
            'id': o.id,
            'date': o.date.isoformat(),
            'kind': o.kind,
            'amount': o.amount,
            'description': o.description,
            'taxable_vat': o.taxable_vat,
            'vat_included': o.vat_included,
            'counterparty': o.counterparty
        } for o in book.ops],
        'employees': [{ 'id': e.id, 'name': e.name, 'gross_salary': e.gross_salary, 'start_date': e.start_date.isoformat() if e.start_date else None } for e in book.employees]
    }
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_book(filename: str = SAMPLE_DATA_FILE) -> AccountBook:
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return create_sample_book()
    b = AccountBook(company_type=data.get('company_type','ip'), regime=data.get('regime','general'))
    for o in data.get('ops',[]):
        b.add_operation(Operation(o.get('id',0), date.fromisoformat(o['date']), o['kind'], o['amount'], o.get('description',''), o.get('taxable_vat',True), o.get('vat_included',False), o.get('counterparty')))
    for e in data.get('employees',[]):
        b.add_employee(Employee(e['id'], e['name'], e['gross_salary'], date.fromisoformat(e['start_date']) if e.get('start_date') else None))
    return b

# CLI

def cli_main():
    parser = argparse.ArgumentParser(description='AI-бухгалтер Казахстан (Алматы)')
    parser.add_argument('--serve', action='store_true', help='Запустить веб-API (FastAPI)')
    parser.add_argument('--export-xlsx', type=str, help='Экспорт операций в Excel (требует pandas)')
    parser.add_argument('--export-csv', type=str, help='Экспорт операций в CSV')
    parser.add_argument('--show-report', action='store_true', help='Показать сводный отчёт')
    parser.add_argument('--generate-recurring', action='store_true', help='Добавить пример периодических операций')
    args = parser.parse_args()

    book = load_book()

    if args.generate_recurring:
        template = Operation(0, date.today(), 'expense', 50_000, 'Аренда офиса (ежемесячно)', taxable_vat=False)
        book.generate_recurring(template, date(2025,1,1), date(2025,12,1), 1)
        save_book(book)
        print('Добавлены периодические операции (аренда).')

    if args.export_csv:
        book.export_csv(args.export_csv)
        print('Экспортировано в', args.export_csv)

    if args.export_xlsx:
        if not HAS_PANDAS:
            print('Ошибка: для экспорта в Excel требуется pandas. Установите: pip install pandas openpyxl')
        else:
            book.export_excel(args.export_xlsx)
            print('Экспортировано в', args.export_xlsx)

    if args.show_report:
        r = book.summary_report()
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        print('\nБлижайшие сроки:')
        for d, desc in book.upcoming_deadlines():
            print(f"{d.isoformat()} — {desc}")

    if args.serve:
        if not HAS_FASTAPI:
            print('Ошибка: FastAPI/uvicorn не установлены. Установите: pip install fastapi uvicorn')
            return
        app = FastAPI()

        @app.get('/api/report')
        def api_report(start: Optional[str]=None, end: Optional[str]=None):
            s = date.fromisoformat(start) if start else None
            e = date.fromisoformat(end) if end else None
            return JSONResponse(book.summary_report(s,e))

        @app.get('/api/ops')
        def api_ops():
            return JSONResponse([{
                'id': o.id,
                'date': o.date.isoformat(),
                'kind': o.kind,
                'amount': o.amount,
                'description': o.description
            } for o in book.ops])

        @app.post('/api/op')
        def api_add_op(payload: Dict[str, Any]):
            # minimal validation
            dt = date.fromisoformat(payload.get('date')) if payload.get('date') else date.today()
            kind = payload.get('kind','income')
            amt = float(payload.get('amount',0))
            desc = payload.get('description','')
            taxable = payload.get('taxable_vat', True)
            vat_inc = payload.get('vat_included', False)
            book.add_operation(Operation(0, dt, kind, amt, desc, taxable, vat_inc, payload.get('counterparty')))
            save_book(book)
            return JSONResponse({'status':'ok'})

        print('Запуск веб-API на http://127.0.0.1:8000')
        uvicorn.run(app, host='127.0.0.1', port=8000)

    # если не запущен сервер и нет аргументов — выводим краткий отчёт
    if not any([args.serve, args.export_csv, args.export_xlsx, args.show_report, args.generate_recurring]):
        print('AI-Бухгалтер (локально) — пример использования')
        print('Запустите с --show-report для полной сводки, --serve для веб-API (опционально).')

if __name__ == '__main__':
    cli_main()
