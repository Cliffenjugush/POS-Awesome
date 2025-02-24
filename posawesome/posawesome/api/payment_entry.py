# Copyright (c) 2021, Youssef Restom and contributors
# For license information, please see license.txt

import frappe, erpnext, json, re
from frappe import _
from frappe.utils import nowdate, getdate, flt
from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_account_currency
from erpnext.accounts.doctype.journal_entry.journal_entry import (
    get_default_bank_cash_account,
)
from erpnext.setup.utils import get_exchange_rate
from erpnext.accounts.doctype.bank_account.bank_account import get_party_bank_account
from posawesome.posawesome.api.m_pesa import submit_mpesa_payment
from erpnext.accounts.utils import QueryPaymentLedger, get_outstanding_invoices as _get_outstanding_invoices


def create_payment_entry(
    company,
    customer,
    amount,
    currency,
    mode_of_payment,
    reference_date=None,
    reference_no=None,
    posting_date=None,
    cost_center=None,
    submit=0,
):
    # TODO : need to have a better way to handle currency
    date = nowdate() if not posting_date else posting_date
    party_type = "Customer"
    party_account = get_party_account(party_type, customer, company)
    party_account_currency = get_account_currency(party_account)
    if party_account_currency != currency:
        frappe.throw(
            _(
                "Currency is not correct, party account currency is {party_account_currency} and transaction currency is {currency}"
            ).format(party_account_currency=party_account_currency, currency=currency)
        )
    payment_type = "Receive"

    bank = get_bank_cash_account(company, mode_of_payment)
    company_currency = frappe.get_value("Company", company, "default_currency")
    conversion_rate = get_exchange_rate(currency, company_currency, date, "for_selling")
    paid_amount, received_amount = set_paid_amount_and_received_amount(
        party_account_currency, bank, amount, payment_type, None, conversion_rate
    )

    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = payment_type
    pe.company = company
    pe.cost_center = cost_center or erpnext.get_default_cost_center(company)
    pe.posting_date = date
    pe.mode_of_payment = mode_of_payment
    pe.party_type = party_type
    pe.party = customer

    pe.paid_from = party_account if payment_type == "Receive" else bank.account
    pe.paid_to = party_account if payment_type == "Pay" else bank.account
    pe.paid_from_account_currency = (
        party_account_currency if payment_type == "Receive" else bank.account_currency
    )
    pe.paid_to_account_currency = (
        party_account_currency if payment_type == "Pay" else bank.account_currency
    )
    pe.paid_amount = paid_amount
    pe.received_amount = received_amount
    pe.letter_head = frappe.get_value("Company", company, "default_letter_head")
    pe.reference_date = reference_date
    pe.reference_no = reference_no
    if pe.party_type in ["Customer", "Supplier"]:
        bank_account = get_party_bank_account(pe.party_type, pe.party)
        pe.set("bank_account", bank_account)
        pe.set_bank_account_data()

    pe.setup_party_account_field()
    pe.set_missing_values()

    if party_account and bank:
        pe.set_amounts()
    if submit:
        pe.docstatus = 1
    pe.insert(ignore_permissions=True)
    return pe


def get_bank_cash_account(company, mode_of_payment, bank_account=None):
    bank = get_default_bank_cash_account(
        company, "Bank", mode_of_payment=mode_of_payment, account=bank_account
    )

    if not bank:
        bank = get_default_bank_cash_account(
            company, "Cash", mode_of_payment=mode_of_payment, account=bank_account
        )

    return bank


def set_paid_amount_and_received_amount(
    party_account_currency,
    bank,
    outstanding_amount,
    payment_type,
    bank_amount,
    conversion_rate,
):
    paid_amount = received_amount = 0
    if party_account_currency == bank.account_currency:
        paid_amount = received_amount = abs(outstanding_amount)
    elif payment_type == "Receive":
        paid_amount = abs(outstanding_amount)
        if bank_amount:
            received_amount = bank_amount
        else:
            received_amount = paid_amount * conversion_rate
    else:
        received_amount = abs(outstanding_amount)
        if bank_amount:
            paid_amount = bank_amount
        else:
            # if party account currency and bank currency is different then populate paid amount as well
            paid_amount = received_amount * conversion_rate

    return paid_amount, received_amount


def mask_mobile(mobile):
    """
    Masks a mobile number and extracts the last 3 digits for partial matching:
    - For M-Pesa masked numbers (e.g., "247*****039"): Returns "247*****039" and "039"
    - For unmasked numbers starting with 0 (e.g., "0712345039"): Returns "7*****039" and "039"
    - For unmasked numbers starting with 254 (e.g., "254712345039"): Returns "2547*****039" and "039"
    """
    if not mobile:
        return "", ""
    mobile = str(mobile).strip()
    last_digits = mobile[-3:] if len(mobile) >= 3 else mobile  # Last 3 digits for comparison
    if re.match(r'^\d{3}\*{5}\d{3}$', mobile):  # M-Pesa masked format (e.g., "247*****039")
        masked = mobile
    elif mobile.startswith('0'):
        # For numbers starting with 0 (e.g., "0712345039")
        mobile = mobile[1:]  # Remove leading 0
        masked = mobile[:1] + '*****' + mobile[6:] if len(mobile) > 7 else mobile
    elif mobile.startswith('254'):
        # For numbers starting with 254
        masked = mobile[:4] + '*****' + mobile[9:] if len(mobile) > 13 else mobile
    else:
        masked = mobile  # Return as is if format not recognized
    return masked, last_digits


@frappe.whitelist()
def get_outstanding_invoices(company, currency, customers=None, pos_profile_name=None):
    """
    Fetches outstanding invoices for all customers or optionally filtered by specific customers and/or POS profile.

    :param company: The company for which invoices are being fetched.
    :param currency: The currency in which invoices are filtered.
    :param customers: Optional list of customer names to filter invoices.
    :param pos_profile_name: Optional POS profile name to further filter invoices.
    :return: A list of dictionaries containing invoice details.
    """
    # Initialize filters
    filters = {
        "company": company,
        "currency": currency,
        "outstanding_amount": [">", 0],
        "docstatus": 1,  # Only submitted invoices
    }

    # Add customer filter if customers are provided and not empty
    if customers:
        if isinstance(customers, str):
            customers = json.loads(customers)  # Convert string to list if necessary
        if customers:  # Ensure the list is not empty
            filters["customer"] = ["in", customers]

    # Add POS profile filter if provided
    if pos_profile_name:
        filters["pos_profile"] = pos_profile_name

    # Fetch invoices based on the defined filters
    invoices = frappe.get_all(
        "Sales Invoice",
        filters=filters,
        fields=[
            "name",
            "customer",
            "customer_name",
            "posting_date",
            "due_date",
            "grand_total",
            "outstanding_amount",
            "currency",
            "pos_profile",
        ],
        order_by="posting_date desc",  # Order by posting date in descending order
    )

    return invoices


@frappe.whitelist()
def get_customers():
    customers = frappe.get_all(
        "Customer",
        fields=["name", "customer_name", "mobile_no"],
        filters={"disabled": 0},
        order_by="customer_name asc",
        limit_page_length=0
    )
    return customers


@frappe.whitelist()
def get_unallocated_payments(customers, company, currency, mode_of_payment=None):
    if isinstance(customers, str):
        customers = json.loads(customers)
    filters = {
        "party": ["in", customers],
        "company": company,
        "docstatus": 1,
        "party_type": "Customer",
        "payment_type": "Receive",
        "unallocated_amount": [">", 0],
        "paid_from_account_currency": currency,
    }
    if mode_of_payment:
        filters.update({"mode_of_payment": mode_of_payment})
    unallocated_payment = frappe.get_all(
        "Payment Entry",
        filters=filters,
        fields=[
            "name",
            "paid_amount",
            "party_name as customer_name",
            "received_amount",
            "posting_date",
            "unallocated_amount",
            "mode_of_payment",
            "paid_from_account_currency as currency",
        ],
        order_by="posting_date asc",
    )
    return unallocated_payment

@frappe.whitelist()
def process_pos_payment(payload):
    data = json.loads(payload)
    data = frappe._dict(data)

    # Validate required fields
    if isinstance(data.pos_profile, str):
        profile_name = data.pos_profile
        data.pos_profile = frappe.get_doc("POS Profile", profile_name)
        if not data.get("pos_profile_name"):
            data.pos_profile_name = profile_name

    if data.get("pos_opening_shift") and not data.get("pos_opening_shift_name"):
        data.pos_opening_shift_name = data.pos_opening_shift

    if not data.get("customers"):
        data.customers = []

    if not data.pos_profile.get("posa_use_pos_awesome_payments"):
        frappe.throw(_("POS Awesome Payments is not enabled for this POS Profile"))

    if not data.company:
        frappe.throw(_("Company is required"))
    if not data.currency:
        frappe.throw(_("Currency is required"))
    if not data.pos_profile_name:
        frappe.throw(_("POS Profile is required"))
    if not data.pos_opening_shift_name:
        frappe.throw(_("POS Opening Shift is required"))

    company = data.company
    currency = data.currency
    pos_opening_shift_name = data.pos_opening_shift_name
    today = nowdate()

    allow_make_new_payments = data.pos_profile.get("posa_allow_make_new_payments")
    allow_reconcile_payments = data.pos_profile.get("posa_allow_reconcile_payments")
    allow_mpesa_reconcile_payments = data.pos_profile.get("posa_allow_mpesa_reconcile_payments")

    results = []
    all_results_msg = "<h2>Payment Processing Results</h2>"

    for customer_data in data.customers:
        customer = customer_data.get("customer")
        if not customer:
            results.append({
                "customer": None,
                "status": "Error",
                "message": _("Customer is required"),
                "new_payments_entry": [],
                "all_payments_entry": [],
                "errors": [_("Customer is required")],
                "reconcile_doc": None
            })
            continue

        selected_invoices = customer_data.get("selected_invoices", [])
        selected_payments = customer_data.get("selected_payments", [])
        payment_methods = customer_data.get("payment_methods", [])
        selected_mpesa_payments = customer_data.get("selected_mpesa_payments", [])

        new_payments_entry = []
        all_payments_entry = []
        errors = []
        reconcile_doc = None
        updated_invoices = []

        try:
            customer_mobile = frappe.get_value("Customer", customer, "mobile_no") or ""
            customer_name = frappe.get_value("Customer", customer, "customer_name") or ""

            hashed_customer_mobile = ""
            if customer_mobile:
                mobile = customer_mobile.replace("+", "")
                if mobile.startswith("0"):
                    mobile = mobile[1:]
                    hashed_customer_mobile = f"{mobile[0]} ***** {mobile[6:]}" if len(mobile) > 6 else mobile
                elif mobile.startswith("254"):
                    hashed_customer_mobile = f"{mobile[:4]} ***** {mobile[9:]}" if len(mobile) > 9 else mobile
            customer_first_name = customer_name.split(" ")[0].lower() if customer_name else ""

            # Process M-PESA payments
            if (
                allow_mpesa_reconcile_payments and
                selected_mpesa_payments and
                sum(flt(p.get("amount", 0)) for p in selected_mpesa_payments) > 0
            ):
                for mpesa_payment in selected_mpesa_payments:
                    try:
                        amount = flt(mpesa_payment.get("amount", 0))
                        mpesa_mobile = mpesa_payment.get("mobile_no", "") or ""
                        mpesa_full_name = mpesa_payment.get("full_name", "").strip().lower() if mpesa_payment.get("full_name") else ""
                        mpesa_name = mpesa_payment.get("name", "")

                        if amount <= 0:
                            errors.append(f"M-Pesa payment {mpesa_name} has zero or invalid amount")
                            continue

                        is_match = False
                        if hashed_customer_mobile and mpesa_mobile and hashed_customer_mobile == mpesa_mobile:
                            is_match = True
                        elif customer_first_name and mpesa_full_name and customer_first_name == mpesa_full_name:
                            is_match = True

                        if is_match:
                            new_mpesa_payment = submit_mpesa_payment(
                                mpesa_name, customer
                            )
                            if new_mpesa_payment and new_mpesa_payment.doctype == "Payment Entry":
                                if not new_mpesa_payment.unallocated_amount:
                                    new_mpesa_payment.unallocated_amount = flt(new_mpesa_payment.paid_amount or new_mpesa_payment.amount or amount)
                                    new_mpesa_payment.save(ignore_permissions=True)
                                    new_mpesa_payment.submit()  # Ensure payment is submitted
                                new_payments_entry.append(new_mpesa_payment)
                                all_payments_entry.append(new_mpesa_payment.as_dict())
                    except Exception as e:
                        errors.append(f"M-PESA payment failed for {mpesa_payment.get('name')}: {str(e)}")

            # Link payments to invoices using Payment Reconciliation
            has_invoices = len(selected_invoices) > 0
            has_payments = len(all_payments_entry) > 0

            unreconciled_invoices = selected_invoices.copy()
            unreconciled_payments = all_payments_entry.copy()

            if has_invoices and has_payments:
                try:
                    reconcile_doc = frappe.new_doc("Payment Reconciliation")
                    reconcile_doc.party_type = "Customer"
                    reconcile_doc.party = customer
                    reconcile_doc.company = company
                    reconcile_doc.receivable_payable_account = get_party_account("Customer", customer, company)
                    reconcile_doc.get_unreconciled_entries()

                    args = {
                        "invoices": [],
                        "payments": []
                    }
                    for invoice in selected_invoices:
                        grand_total = flt(invoice.get("grand_total", 0))
                        outstanding_amount = flt(invoice.get("outstanding_amount", 0))
                        if grand_total == 0 or outstanding_amount == 0:
                            errors.append(f"Invoice {invoice.get('name')} missing grand_total or outstanding_amount")
                            continue
                        args["invoices"].append({
                            "invoice_type": "Sales Invoice",
                            "invoice_number": invoice.get("name"),
                            "invoice_date": invoice.get("posting_date"),
                            "amount": grand_total,
                            "outstanding_amount": outstanding_amount,
                            "currency": invoice.get("currency", currency),
                            "exchange_rate": 0
                        })
                    for payment in all_payments_entry:
                        payment_amount = flt(payment.get("unallocated_amount", payment.get("paid_amount", 0)))
                        if payment_amount == 0:
                            errors.append(f"Payment {payment.get('name')} has no unallocated amount")
                            continue
                        args["payments"].append({
                            "reference_type": "Payment Entry",
                            "reference_name": payment.get("name"),
                            "posting_date": payment.get("posting_date"),
                            "amount": payment_amount,
                            "unallocated_amount": payment_amount,
                            "difference_amount": 0,
                            "currency": payment.get("currency", currency),
                            "exchange_rate": 0
                        })

                    if not args["invoices"] or not args["payments"]:
                        errors.append("No valid invoices or payments for reconciliation")
                    else:
                        reconcile_doc.allocate_entries(args)
                        reconcile_doc.reconcile()
                        reconcile_doc.save(ignore_permissions=True)
                        reconcile_doc.submit()  # Ensure reconciliation is committed

                        # Verify updated invoices
                        for invoice in selected_invoices:
                            si = frappe.get_doc("Sales Invoice", invoice.get("name"))
                            if flt(si.outstanding_amount) < flt(invoice.get("outstanding_amount", 0)):
                                updated_invoices.append({
                                    "name": invoice.get("name"),
                                    "original_amount": flt(invoice.get("outstanding_amount", 0)),
                                    "remaining_amount": flt(si.outstanding_amount)
                                })
                                unreconciled_invoices = [i for i in unreconciled_invoices if i.get("name") != invoice.get("name")]

                        # Check for unreconciled payments
                        for payment in all_payments_entry:
                            pe = frappe.get_doc("Payment Entry", payment.get("name"))
                            if flt(pe.unallocated_amount) == 0:
                                unreconciled_payments = [p for p in unreconciled_payments if p.get("name") != payment.get("name")]

                except Exception as e:
                    errors.append(f"Reconciliation failed for {customer}: {str(e)}")

            status = "Success" if not errors and (new_payments_entry or updated_invoices) else "Error"

            # Append results for this customer
            results.append({
                "customer": customer,
                "status": status,
                "new_payments_entry": [pe.as_dict() for pe in new_payments_entry],
                "all_payments_entry": all_payments_entry,
                "updated_invoices": updated_invoices,
                "errors": errors,
                "reconcile_doc": reconcile_doc.name if reconcile_doc else None,
                "selected_mpesa_payments": selected_mpesa_payments,
                "unreconciled_invoices": unreconciled_invoices
            })

        except Exception as e:
            errors.append(f"Unexpected error processing customer {customer}: {str(e)}")
            results.append({
                "customer": customer,
                "status": "Error",
                "new_payments_entry": [],
                "all_payments_entry": [],
                "updated_invoices": [],
                "errors": errors,
                "reconcile_doc": None,
                "selected_mpesa_payments": selected_mpesa_payments,
                "unreconciled_invoices": selected_invoices
            })

    # Consolidated and deduplicated tables
    # New Payment Entries Table
    all_new_payments = []
    for result in results:
        all_new_payments.extend(result.get("new_payments_entry", []))
    if all_new_payments:
        all_results_msg += "<h4>New Payment Entries</h4>"
        all_results_msg += "<table class='table table-bordered'><thead><tr><th>Payment Entry</th><th>Amount</th></tr></thead><tbody>"
        for payment in all_new_payments:
            all_results_msg += f"<tr><td>{payment.get('name')}</td><td>{payment.get('paid_amount')}</td></tr>"
        all_results_msg += "</tbody></table>"

    # Reconciled Invoices Table
    all_updated_invoices = []
    for result in results:
        all_updated_invoices.extend(result.get("updated_invoices", []))
    if all_updated_invoices:
        all_results_msg += "<h4>Reconciled Invoices</h4>"
        all_results_msg += "<table class='table table-bordered'><thead><tr><th>Invoice</th><th>Original Amount</th><th>Reconciled Amount</th><th>Remaining Amount</th></tr></thead><tbody>"
        for invoice in all_updated_invoices:
            reconciled_amount = invoice["original_amount"] - invoice["remaining_amount"]
            all_results_msg += f"<tr><td>{invoice['name']}</td><td>{invoice['original_amount']}</td><td>{reconciled_amount}</td><td>{invoice['remaining_amount']}</td></tr>"
        all_results_msg += "</tbody></table>"
    # else:
    #     all_results_msg += "<h4>Reconciled Invoices</h4><p>No invoices reconciled</p>"

    # Reconciled M-PESA Payments Table (only processed ones)
    mpesa_payments_dict = {}
    for result in results:
        for mpesa in result.get("selected_mpesa_payments", []):
            mpesa_id = mpesa.get("name")
            processed = any(p.get("mpesa_reference") == mpesa_id for p in result.get("new_payments_entry", []))
            if processed:
                if mpesa_id in mpesa_payments_dict:
                    mpesa_payments_dict[mpesa_id] += flt(mpesa.get("amount", 0))
                else:
                    mpesa_payments_dict[mpesa_id] = flt(mpesa.get("amount", 0))
    # if mpesa_payments_dict:
    #     all_results_msg += "<h4>Reconciled M-PESA Payments</h4>"
    #     all_results_msg += "<table class='table table-bordered'><thead><tr><th>Transaction ID</th><th>Total Amount</th></tr></thead><tbody>"
    #     for mpesa_id, amount in mpesa_payments_dict.items():
    #         all_results_msg += f"<tr><td>{mpesa_id}</td><td>{amount}</td></tr>"
    #     all_results_msg += "</tbody></table>"
    # else:
    #     all_results_msg += "<h4>Reconciled M-PESA Payments</h4><p>No M-PESA payments reconciled</p>"

    # Unreconciled Invoices Table
    all_unreconciled_invoices = {}
    for result in results:
        for invoice in result.get("Updated Invoices", []):
            invoice_id = invoice.get("name")
            if invoice_id not in all_unreconciled_invoices:
                all_unreconciled_invoices[invoice_id] = flt(invoice.get("outstanding_amount"))
    if all_unreconciled_invoices:
        all_results_msg += "<h4>Unreconciled Invoices</h4>"
        all_results_msg += "<table class='table table-bordered'><thead><tr><th>Invoice</th><th>Outstanding Amount</th></tr></thead><tbody>"
        for invoice_id, outstanding in all_unreconciled_invoices.items():
            if outstanding > 0:
                all_results_msg += f"<tr><td>{invoice_id}</td><td>{outstanding}</td></tr>"
        all_results_msg += "</tbody></table>"
    # else:
    #     all_results_msg += "<h4>Unreconciled Invoices</h4><p>All invoices reconciled</p>"

    # Unreconciled M-PESA Payments Table
    # unreconciled_mpesa_dict = {}
    # for result in results:
    #     for mpesa in result.get("selected_mpesa_payments", []):
    #         mpesa_id = mpesa.get("name")
    #         if not any(p.get("mpesa_reference") == mpesa_id for p in result.get("new_payments_entry", [])):
    #             if mpesa_id in unreconciled_mpesa_dict:
    #                 unreconciled_mpesa_dict[mpesa_id] += flt(mpesa.get("amount", 0))
    #             else:
    #                 unreconciled_mpesa_dict[mpesa_id] = flt(mpesa.get("amount", 0))
    # if unreconciled_mpesa_dict:
    #     all_results_msg += "<h4>Reconciled M-PESA Payments</h4>"
    #     all_results_msg += "<table class='table table-bordered'><thead><tr><th>Transaction ID</th><th>Total Amount</th></tr></thead><tbody>"
    #     for mpesa_id, amount in unreconciled_mpesa_dict.items():
    #         all_results_msg += f"<tr><td>{mpesa_id}</td><td>{amount}</td></tr>"
    #     all_results_msg += "</tbody></table>"
    # else:
    #     all_results_msg += "<h4>Unreconciled M-PESA Payments</h4><p>All M-PESA payments processed</p>"

    # Display the results
    frappe.msgprint(
        msg=all_results_msg,
        title=_("Payment Processing Results"),
        indicator="green" if not any(r["status"] == "Error" for r in results) else "orange",
        wide=True
    )

    return {"results": results}

@frappe.whitelist()
def get_available_pos_profiles(company, currency):
    pos_profiles_list = frappe.get_list(
        "POS Profile",
        filters={"disabled": 0, "company": company, "currency": currency},
        page_length=1000,
        pluck="name",
    )
    return pos_profiles_list