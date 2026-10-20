import json
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd


class BankAccount:
    """Банковский счёт с сохранением баланса и истории операций."""

    DATA_FILE = Path(__file__).with_name("accounts.json")
    NAME_PATTERN = re.compile(
        r"(?:[А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+|[A-Z][a-z]+ [A-Z][a-z]+)"
    )

    def __init__(self, owner, initial_balance=0, account_type="checking"):
        if not self.NAME_PATTERN.fullmatch(owner):
            raise ValueError("Имя должно быть в формате «Имя Фамилия»")
        if initial_balance < 0:
            raise ValueError("Начальный баланс не может быть отрицательным")
        if account_type not in ("checking", "savings"):
            raise ValueError("Тип счёта должен быть checking или savings")

        data = self._read_data()
        number = data["next_account_number"]

        self.owner = owner
        self.account_number = f"ACC-{number:04d}"
        self.account_type = account_type
        self.balance = initial_balance
        self.history = []

        data["next_account_number"] += 1
        data["accounts"][self.account_number] = self._to_dict()
        self._write_data(data)

    def deposit(self, amount):
        self._validate_amount(amount)
        self.balance += amount
        self._record_operation("deposit", amount, "success")
        self._save()

    def withdraw(self, amount):
        self._validate_amount(amount)
        can_withdraw, reason = self._can_withdraw(amount)

        if can_withdraw:
            self.balance -= amount
            status = "success"
        else:
            status = "fail"
            if reason == "exceeds_50_percent_limit":
                print("Операция отклонена. Нельзя снять больше 50% баланса")
            else:
                print(f"Операция отклонена. Недостаточно средств на балансе: {self.balance}")

        self._record_operation("withdraw", amount, status, reason=reason)
        self._save()
        return can_withdraw

    def add_interest(self, rate):
        if self.account_type != "savings":
            raise ValueError("Проценты можно начислить только на сберегательный счёт")
        if rate <= 0:
            raise ValueError("Процентная ставка должна быть положительной")

        interest = round(self.balance * rate / 100, 2)
        self.balance += interest
        self._record_operation(
            "interest",
            interest,
            "success",
            rate=rate,
        )
        self._save()
        return interest

    def transfer_to(self, receiver, amount):
        self._validate_amount(amount)
        if not isinstance(receiver, BankAccount):
            raise TypeError("Получатель должен быть банковским счётом")

        transfer_id = uuid4().hex[:8]
        can_withdraw, reason = self._can_withdraw(amount)

        if not can_withdraw:
            self._record_operation(
                "transfer_out",
                amount,
                "fail",
                transfer_id=transfer_id,
                related_account=receiver.account_number,
                reason=reason,
            )
            self._save()
            return None

        self.balance -= amount
        receiver.balance += amount

        self._record_operation(
            "transfer_out",
            amount,
            "success",
            transfer_id=transfer_id,
            related_account=receiver.account_number,
        )
        receiver._record_operation(
            "transfer_in",
            amount,
            "success",
            transfer_id=transfer_id,
            related_account=self.account_number,
        )
        self._save()
        receiver._save()
        return transfer_id

    def get_balance(self):
        return self.balance

    def get_history(self):
        history = pd.DataFrame(self.history)
        if not history.empty:
            history["datetime"] = pd.to_datetime(history["datetime"])
        return history

    def get_large_operations(self, min_amount, n):
        if min_amount <= 0 or n <= 0:
            raise ValueError("Порог и количество операций должны быть положительными")

        history = self.get_history()
        if history.empty:
            return history

        large_operations = history[
            (history["status"] == "success")
            & (history["amount"] >= min_amount)
        ]
        return large_operations.tail(n).reset_index(drop=True)

    def load_operations(self, file_path, source_account_number):
        file_path = Path(file_path)

        if file_path.suffix.lower() == ".csv":
            data = pd.read_csv(file_path)
        elif file_path.suffix.lower() == ".json":
            data = pd.read_json(file_path)
        else:
            raise ValueError("Поддерживаются только файлы CSV и JSON")

        required_columns = {
            "account_number",
            "account_type",
            "date",
            "operation",
            "amount",
            "balance_after",
            "status",
        }
        if not required_columns.issubset(data.columns):
            raise ValueError("В файле отсутствуют обязательные столбцы")

        account_rows = data[
            data["account_number"].astype(str) == source_account_number
        ]
        skipped = len(data) - len(account_rows)
        accepted = 0
        rejected_rows = []

        allowed_operations = {"deposit", "withdraw"}
        if self.account_type == "savings":
            allowed_operations.add("interest")

        for row_index, row in account_rows.iterrows():
            reason = None

            if str(row["account_type"]).strip() != self.account_type:
                reason = "тип счёта не совпадает"

            operation = row["operation"]
            if reason is None and (pd.isna(operation) or not str(operation).strip()):
                reason = "пустой тип операции"
            elif reason is None:
                operation = str(operation).strip()
                if operation not in {"deposit", "withdraw", "interest"}:
                    reason = f"неизвестная операция: {operation}"
                elif operation not in allowed_operations:
                    reason = f"операция {operation} недопустима для {self.account_type}"

            amount = pd.to_numeric(row["amount"], errors="coerce")
            if reason is None and pd.isna(amount):
                reason = "пустая или некорректная сумма"
            elif reason is None and amount <= 0:
                reason = "сумма должна быть положительной"

            operation_date = pd.to_datetime(
                row["date"],
                errors="coerce",
                format="mixed",
                dayfirst=True,
            )
            if reason is None and pd.isna(operation_date):
                reason = "некорректная дата"

            status = str(row["status"]).strip()
            if reason is None and status not in {"success", "fail"}:
                reason = f"некорректный статус: {status}"

            source_balance = pd.to_numeric(row["balance_after"], errors="coerce")
            if reason is None and (pd.isna(source_balance) or source_balance < 0):
                reason = "некорректный баланс после операции"

            if reason is None and status == "success" and operation == "withdraw":
                can_withdraw, withdraw_reason = self._can_withdraw(amount)
                if not can_withdraw:
                    if withdraw_reason == "exceeds_50_percent_limit":
                        reason = "превышен лимит снятия 50%"
                    else:
                        reason = "недостаточно средств"

            if reason is not None:
                rejected_rows.append(
                    {
                        "row_index": row_index,
                        "operation": row["operation"],
                        "amount": row["amount"],
                        "date": row["date"],
                        "status": row["status"],
                        "reason": reason,
                    }
                )
                continue

            if status == "success":
                if operation in {"deposit", "interest"}:
                    self.balance = round(self.balance + amount, 2)
                else:
                    self.balance = round(self.balance - amount, 2)

            self._record_operation(
                operation,
                amount,
                status,
                operation_datetime=operation_date,
                source_account_number=source_account_number,
            )
            accepted += 1

        if accepted:
            self._save()

        return {
            "accepted": accepted,
            "rejected": len(rejected_rows),
            "skipped_other_accounts": skipped,
            "rejected_rows": pd.DataFrame(rejected_rows),
        }

    def _can_withdraw(self, amount):
        if self.account_type == "savings" and amount > self.balance * 0.5:
            return False, "exceeds_50_percent_limit"
        if amount > self.balance:
            return False, "insufficient_funds"
        return True, None

    def _record_operation(
        self,
        operation,
        amount,
        status,
        operation_datetime=None,
        **details,
    ):
        if operation_datetime is None:
            operation_datetime = datetime.now()

        record = {
            "operation": operation,
            "amount": amount,
            "datetime": operation_datetime.isoformat(timespec="seconds"),
            "balance_after": self.balance,
            "status": status,
        }
        record.update(details)
        self.history.append(record)

    @staticmethod
    def _validate_amount(amount):
        if amount <= 0:
            raise ValueError("Сумма операции должна быть положительной")

    @classmethod
    def load(cls, account_number):
        data = cls._read_data()
        if account_number not in data["accounts"]:
            raise ValueError("Счёт с таким номером не найден")

        saved_account = data["accounts"][account_number]
        account = cls.__new__(cls)
        account.owner = saved_account["owner"]
        account.account_number = account_number
        account.account_type = saved_account.get("account_type", "checking")
        account.balance = saved_account["balance"]
        account.history = saved_account["history"]
        return account

    def _to_dict(self):
        return {
            "owner": self.owner,
            "account_type": self.account_type,
            "balance": self.balance,
            "history": self.history,
        }

    def _save(self):
        data = self._read_data()
        data["accounts"][self.account_number] = self._to_dict()
        self._write_data(data)

    @classmethod
    def _read_data(cls):
        with open(cls.DATA_FILE, "r", encoding="utf-8") as file:
            return json.load(file)

    @classmethod
    def _write_data(cls, data):
        with open(cls.DATA_FILE, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=4)
