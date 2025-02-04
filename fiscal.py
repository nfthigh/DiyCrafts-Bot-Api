# fiscal.py
from products import products_data

def create_fiscal_item(product_name: str, quantity: int, unit_price: float) -> dict:
    """
    Формирует элемент фискальных данных для платежа.
    
    :param product_name: Название товара (например, "Кружка")
    :param quantity: Количество товара
    :param unit_price: Цена за единицу, заданная администратором
    :return: Словарь с фискальными данными
    """
    product = products_data.get(product_name)
    if not product:
        raise ValueError(f"Товар '{product_name}' не найден")
    price_total = unit_price * quantity
    # Пример расчета НДС: если сумма включает НДС 12%
    vat = round((price_total / 1.12) * 0.12)
    fiscal_item = {
        "Name": product_name,
        "SPIC": product["SPIC"],
        "PackageCode": product["PackageCode"],
        "GoodPrice": unit_price,   # Цена за единицу (админская)
        "Price": price_total,        # Общая стоимость
        "Amount": quantity,
        "VAT": vat,
        "VATPercent": 12,
        "CommissionInfo": product["CommissionInfo"]
    }
    return fiscal_item

# Тестовый вызов (при необходимости)
if __name__ == "__main__":
    item = create_fiscal_item("Кружка", 2, 50000)
    print(item)
