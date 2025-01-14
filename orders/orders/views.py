from datetime import time, datetime

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.validators import URLValidator
from django.db import IntegrityError
from django.db.models import Q, Sum, F
from django.http import JsonResponse
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import ValidationError
from requests import get
from yaml import load as load_yaml, Loader
from ujson import loads as load_json

from orders.models import (
    Shop,
    Category,
    ProductInfo,
    Product,
    Parameter,
    ProductParameter,
    Order,
    OrderItem,
    Contact,
)
from orders.serializers import (
    UserSerializer,
    ProductInfoSerializer,
    OrderSerializer,
    OrderItemSerializer,
    ContactSerializer,
)
from orders.settings import MEDIA_ROOT
from orders.signals import new_order, new_order_to_shop, new_user_registered


class RegisterAccount(APIView):
    """
    Для регистрации покупателей
    """

    # Регистрация методом POST
    def post(self, request, *args, **kwargs):
        # проверяем обязательные аргументы
        if {
            "first_name",
            "last_name",
            "email",
            "password",
            "company",
            "position",
        }.issubset(request.data):
            errors = {}

            # проверяем пароль на сложность

            try:
                validate_password(request.data["password"])
            except Exception as password_error:
                error_array = []
                # noinspection PyTypeChecker
                for item in password_error:
                    error_array.append(item)
                return JsonResponse(
                    {"Status": False, "Errors": {"password": error_array}}
                )
            else:
                # проверяем данные для уникальности имени пользователя
                request.data._mutable = True
                request.data.update({})
                user_serializer = UserSerializer(data=request.data)
                if user_serializer.is_valid():
                    # сохраняем пользователя
                    user = user_serializer.save()
                    user.set_password(request.data["password"])
                    user.save()
                    new_user_registered.send(sender=self.__class__, user_id=user.id)
                    return JsonResponse({"Status": True})
                else:
                    return JsonResponse(
                        {"Status": False, "Errors": user_serializer.errors}
                    )

        return JsonResponse(
            {"Status": False, "Errors": "Не указаны все необходимые аргументы"}
        )


class LoginAccount(APIView):
    """
    Класс для авторизации пользователей
    """

    # Авторизация методом POST
    def post(self, request, *args, **kwargs):
        if {"email", "password"}.issubset(request.data):
            user = authenticate(
                request,
                username=request.data["email"],
                password=request.data["password"],
            )
            if user is not None:
                if user.is_active:
                    token = Token.objects.get_or_create(user=user)[0]
                    return JsonResponse({"Status": True, "Token": token.key})
            return JsonResponse({"Status": False, "Errors": "Не удалось авторизовать"})
        return JsonResponse(
            {"Status": False, "Errors": "Не указаны все необходимые аргументы"}
        )


class PartnerUpdate(APIView):
    """
    Класс для обновления прайса от поставщика
    """

    def post(self, request, *args, **kwargs):
        # if not request.user.is_authenticated:
        #     return JsonResponse({'Status': False, 'Error': 'Log in required'}, status=403)

        # if request.user.type != 'shop':
        #     return JsonResponse({'Status': False, 'Error': 'Только для магазинов'}, status=403)

        # Load YAML data from the file
        filename = request.data.get("filename")
        if filename:
            with open(f"{MEDIA_ROOT}{filename}.yaml", encoding="UTF-8") as fh:
                data = load_yaml(fh, Loader=Loader)
                # if url:
                #     validate_url = URLValidator()
                #     try:
                #         validate_url(url)
                #     except ValidationError as e:
                #         return JsonResponse({'Status': False, 'Error': str(e)})
                #     else:
                #         stream = get(url).content

                shop = Shop.objects.get_or_create(name=data["shop"])[0]
                for category in data["categories"]:
                    category_object = Category.objects.get_or_create(
                        id=category["id"], name=category["name"]
                    )[0]
                    category_object.shops.add(shop.id)
                    category_object.save()
                ProductInfo.objects.filter(shop_id=shop.id).delete()
                if "goods" in data:
                    for item in data["goods"]:
                        product = Product.objects.get_or_create(
                            name=item["name"], category_id=item["category"]
                        )[0]
                        product_info = ProductInfo.objects.create(
                            product_id=product.id,
                            id=item["id"],
                            # model=item['model'],
                            price=item["price"],
                            price_rrc=item["price_rrc"],
                            quantity=item["quantity"],
                            shop_id=shop.id,
                        )
                        for name, value in item["parameters"].items():
                            parameter_object = Parameter.objects.get_or_create(
                                name=name
                            )[0]
                            ProductParameter.objects.create(
                                product_info_id=product_info.id,
                                parameter_id=parameter_object.id,
                                value=value,
                            )
                return JsonResponse({"Status": True})
        return JsonResponse({"Status": False, "Errors": "Не указано имя файла"})


class ProductInfoView(APIView):
    """
    Класс для поиска товаров
    """

    def get(self, request, *args, **kwargs):
        id = request.query_params.get("id")
        shop_id = request.query_params.get("shop_id")
        category_id = request.query_params.get("category_id")
        query = Q()
        if id:
            query = query & Q(id=id)
        if shop_id:
            query = query & Q(shop_id=shop_id)
        if category_id:
            query = query & Q(product__category_id=category_id)

        # фильтруем и отбрасываем дубликаты
        queryset = (
            ProductInfo.objects.filter(query)
            .select_related("shop", "product__category")
            .prefetch_related("product_parameters__parameter")
            .distinct()
        )

        serializer = ProductInfoSerializer(queryset, many=True)

        return Response(serializer.data)


class BasketView(APIView):
    """
    Класс для работы с корзиной пользователя
    """

    # получить корзину
    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"Status": False, "Error": "Log in required"}, status=403
            )
        basket = (
            Order.objects.filter(user_id=request.user.id, state="basket")
            .prefetch_related(
                "ordered_items__product_info__product__category",
                "ordered_items__product_info__product_parameters__parameter",
            )
            .annotate(
                total_sum=Sum(
                    F("ordered_items__quantity")
                    * F("ordered_items__product_info__price")
                )
            )
            .distinct()
        )

        serializer = OrderSerializer(basket, many=True)
        return Response(serializer.data)

    # редактировать корзину
    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"Status": False, "Error": "Log in required"}, status=403
            )

        items_string = request.data.get("items")
        if items_string:
            try:
                items_dict = load_json(items_string)
            except ValueError:
                return JsonResponse(
                    {"Status": False, "Errors": "Неверный формат запроса"}
                )
            else:
                basket = Order.objects.get_or_create(
                    user_id=request.user.id, state="basket"
                )[0]
                objects_created = 0
                for order_item in items_dict:
                    order_item.update({"order": basket.id})
                    serializer = OrderItemSerializer(data=order_item)
                    if serializer.is_valid():
                        try:
                            serializer.save()
                        except IntegrityError as error:
                            return JsonResponse({"Status": False, "Errors": str(error)})
                        else:
                            objects_created += 1

                    else:
                        return JsonResponse(
                            {"Status": False, "Errors": serializer.errors}
                        )

                return JsonResponse(
                    {"Status": True, "Создано объектов": objects_created}
                )
        return JsonResponse(
            {"Status": False, "Errors": "Не указаны все необходимые аргументы"}
        )

    # удалить товары из корзины
    def delete(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"Status": False, "Error": "Log in required"}, status=403
            )

        items_string = request.data.get("items")
        if items_string:
            items_list = items_string.split(",")
            basket = Order.objects.get_or_create(
                user_id=request.user.id, state="basket"
            )[0]
            query = Q()
            objects_deleted = False
            for order_item_id in items_list:
                if order_item_id.isdigit():
                    query = query | Q(order_id=basket.id, id=order_item_id)
                    objects_deleted = True

            if objects_deleted:
                deleted_count = OrderItem.objects.filter(query).delete()[0]
                return JsonResponse({"Status": True, "Удалено объектов": deleted_count})
        return JsonResponse(
            {"Status": False, "Errors": "Не указаны все необходимые аргументы"}
        )

    # добавить позиции в корзину
    def put(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"Status": False, "Error": "Log in required"}, status=403
            )

        items_string = request.data.get("items")
        if items_string:
            try:
                items_dict = load_json(items_string)
            except ValueError:
                return JsonResponse(
                    {"Status": False, "Errors": "Неверный формат запроса"}
                )
            else:
                basket = Order.objects.get_or_create(
                    user_id=request.user.id, state="basket"
                )[0]
                objects_updated = 0
                for order_item in items_dict:
                    if (
                        type(order_item["id"]) == int
                        and type(order_item["quantity"]) == int
                    ):
                        objects_updated += OrderItem.objects.filter(
                            order_id=basket.id, id=order_item["id"]
                        ).update(quantity=order_item["quantity"])

                return JsonResponse(
                    {"Status": True, "Обновлено объектов": objects_updated}
                )
        return JsonResponse(
            {"Status": False, "Errors": "Не указаны все необходимые аргументы"}
        )


class ContactView(APIView):
    """
    Класс для работы с контактами покупателей
    """

    # получить мои контакты
    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"Status": False, "Error": "Log in required"}, status=403
            )
        contact = Contact.objects.filter(user_id=request.user.id)
        serializer = ContactSerializer(contact, many=True)
        return Response(serializer.data)

    # добавить новый контакт
    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"Status": False, "Error": "Log in required"}, status=403
            )

        if {"city", "street", "phone"}.issubset(request.data):
            request.data._mutable = True
            request.data.update({"user": request.user.id})
            serializer = ContactSerializer(data=request.data)

            if serializer.is_valid():
                serializer.save()
                return JsonResponse({"Status": True})
            else:
                return JsonResponse({"Status": False, "Errors": serializer.errors})

        return JsonResponse(
            {"Status": False, "Errors": "Не указаны все необходимые аргументы"}
        )

    # удалить контакт
    def delete(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"Status": False, "Error": "Log in required"}, status=403
            )

        items_string = request.data.get("items")
        if items_string:
            items_list = items_string.split(",")
            query = Q()
            objects_deleted = False
            for contact_id in items_list:
                if contact_id.isdigit():
                    query = query | Q(user_id=request.user.id, id=contact_id)
                    objects_deleted = True

            if objects_deleted:
                deleted_count = Contact.objects.filter(query).delete()[0]
                return JsonResponse({"Status": True, "Удалено объектов": deleted_count})
        return JsonResponse(
            {"Status": False, "Errors": "Не указаны все необходимые аргументы"}
        )

    # редактировать контакт
    def put(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"Status": False, "Error": "Log in required"}, status=403
            )

        if "id" in request.data:
            if request.data["id"].isdigit():
                contact = Contact.objects.filter(
                    id=request.data["id"], user_id=request.user.id
                ).first()
                if contact:
                    serializer = ContactSerializer(
                        contact, data=request.data, partial=True
                    )
                    if serializer.is_valid():
                        serializer.save()
                        return JsonResponse({"Status": True})
                    else:
                        return JsonResponse(
                            {"Status": False, "Errors": serializer.errors}
                        )

        return JsonResponse(
            {"Status": False, "Errors": "Не указаны все необходимые аргументы"}
        )


class OrderView(APIView):
    """
    Класс для получения и размещения заказов пользователями
    """

    # получить мои заказы
    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"Status": False, "Error": "Log in required"}, status=403
            )
        order = (
            Order.objects.filter(user_id=request.user.id)
            .exclude(state="basket")
            .prefetch_related(
                "ordered_items__product_info__product__category",
                "ordered_items__product_info__product_parameters__parameter",
            )
            .select_related("contact")
            .annotate(
                total_sum=Sum(
                    F("ordered_items__quantity")
                    * F("ordered_items__product_info__price")
                )
            )
            .distinct()
        )

        serializer = OrderSerializer(order, many=True)
        return Response(serializer.data)

    # разместить заказ из корзины
    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"Status": False, "Error": "Log in required"}, status=403
            )
        if {"id", "contact"}.issubset(request.data):
            if request.data["id"].isdigit():
                try:
                    is_updated = Order.objects.filter(
                        user_id=request.user.id, id=request.data["id"], state="basket"
                    ).update(contact_id=request.data["contact"], state="new")
                except IntegrityError as error:
                    return JsonResponse(
                        {"Status": False, "Errors": "Неправильно указаны аргументы"}
                    )
                else:
                    if is_updated:
                        new_order.send(sender=self.__class__, user_id=request.user.id)
                        return JsonResponse({"Status": True})
                    else:
                        return JsonResponse(
                            {"Status": False, "Errors": "Не найдена корзина"}
                        )

        return JsonResponse(
            {"Status": False, "Errors": "Не указаны все необходимые аргументы"}
        )


class PartnerOrders(APIView):
    """
    Класс для получения заказов поставщиками
    """

    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"Status": False, "Error": "Log in required"}, status=403
            )

        if request.user.type != "shop":
            return JsonResponse(
                {"Status": False, "Error": "Только для магазинов"}, status=403
            )

        order = (
            Order.objects.filter(
                ordered_items__product_info__shop__user_id=request.user.id
            )
            .exclude(state="basket")
            .prefetch_related(
                "ordered_items__product_info__product__category",
                "ordered_items__product_info__product_parameters__parameter",
            )
            .select_related("contact")
            .annotate(
                total_sum=Sum(
                    F("ordered_items__quantity")
                    * F("ordered_items__product_info__price")
                )
            )
            .distinct()
        )

        serializer = OrderSerializer(order, many=True)
        return Response(serializer.data)


class OrderNewView(APIView):
    """
    Класс для рассылки заказов администраторам магазинов.
    """

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {"Status": False, "Error": "Log in required"}, status=403
            )
        if not request.user.is_staff:
            return JsonResponse(
                {"Status": False, "Error": "Только для администраторов системы"},
                status=403,
            )
        if {"id"}.issubset(request.data):
            if request.data["id"].isdigit():
                try:
                    is_updated = Order.objects.filter(
                        id=request.data["id"], state="new"
                    ).update(state="confirmed")
                except IntegrityError as error:
                    return JsonResponse(
                        {"Status": False, "Errors": "Неправильно указаны аргументы"}
                    )
                else:
                    if is_updated:
                        print(is_updated)
                        shops = set(
                            [
                                ProductInfo.objects.get(id=item.product_info_id).shop_id
                                for item in OrderItem.objects.filter(
                                    order_id=request.data["id"]
                                )
                            ]
                        )
                        print("=========", shops)
                        for shop in shops:
                            new_order_to_shop.send(
                                sender=self.__class__,
                                shop_id=shop,
                                order_id=request.data["id"],
                            )
                        return JsonResponse({"Status": True})
                    else:
                        return JsonResponse(
                            {"Status": False, "Errors": "Заказ не найден"}
                        )

        return JsonResponse(
            {"Status": False, "Errors": "Не указаны все необходимые аргументы"}
        )
