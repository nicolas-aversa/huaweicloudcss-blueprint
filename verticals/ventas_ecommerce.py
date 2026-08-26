"""Vertical declarativo: media-retail-ecommerce.

Definición única del vertical (card, filtro, campos, capability spec, dashboard
spec, preguntas del chatbot, vocabulario de industria y datasets). Lo consumen
tanto el backend (capabilities/dashboards/industry/datasets) como el frontend
(inyectado por `GET /` como `window.__VERTICALS__`). Ver `verticals/__init__.py`.
"""

VERTICAL = {
    'slug': 'ventas-ecommerce',
    'label': 'Ventas e-commerce',
    'full_label': 'Ventas e-commerce',
    'group': 'retail',
    'icon': 'shopping-cart',
    'index_base': 'ventas-ecommerce',
    'description': 'E-commerce · órdenes, revenue, clientes',
    'sample': '{"order_id":100136,"order_date":"2025-07-01T00:08:46+00:00","status":"COMPLETED","customer_id":18,"customer_full_name":"Clarice Reyes","customer_gender":"FEMALE","category":["Women\'s Shoes","Women\'s Clothing"],"manufacturer":["Tigress Enterprises","Pyramidustries"],"total_quantity":2,"taxful_total_price":45.98,"currency":"EUR","day_of_week":"Tuesday","geo":{"city":"Birmingham","region":"Birmingham","country":"GB"},"products":[{"product_id":15117,"sku":"ZO0019500195","category":"Women\'s Shoes","price":28.99,"quantity":1}]}',
    'filter_code': """filter {
  json { source => "message" }
  date { match => ["order_date", "ISO8601"] target => "@timestamp" }
  mutate { remove_field => ["message"] }
}""",
    'fields': [
        {
            'raw_name': 'order_id',
            'field_path': 'order_id',
            'type': 'keyword',
            'business_label': 'Order ID',
        },
        {
            'raw_name': 'status',
            'field_path': 'status',
            'type': 'keyword',
            'business_label': 'Order Status',
        },
        {
            'raw_name': 'cancel_reason',
            'field_path': 'cancel_reason',
            'type': 'keyword',
            'business_label': 'Cancel Reason',
        },
        {
            'raw_name': 'customer_id',
            'field_path': 'customer_id',
            'type': 'keyword',
            'business_label': 'Customer ID',
        },
        {
            'raw_name': 'customer_gender',
            'field_path': 'customer_gender',
            'type': 'keyword',
            'business_label': 'Gender',
        },
        {
            'raw_name': 'category',
            'field_path': 'category',
            'type': 'keyword',
            'business_label': 'Category',
        },
        {
            'raw_name': 'manufacturer',
            'field_path': 'manufacturer',
            'type': 'keyword',
            'business_label': 'Manufacturer',
        },
        {
            'raw_name': 'total_quantity',
            'field_path': 'total_quantity',
            'type': 'integer',
            'business_label': 'Items',
        },
        {
            'raw_name': 'taxful_total_price',
            'field_path': 'taxful_total_price',
            'type': 'float',
            'business_label': 'Order Total',
        },
        {
            'raw_name': 'city',
            'field_path': 'geo.city',
            'type': 'keyword',
            'business_label': 'City',
        },
        {
            'raw_name': 'country',
            'field_path': 'geo.country',
            'type': 'keyword',
            'business_label': 'Country',
        },
        {
            'raw_name': 'day_of_week',
            'field_path': 'day_of_week',
            'type': 'keyword',
            'business_label': 'Day of Week',
        }
    ],
    'suggested_questions': [
        '¿Cuántos pedidos de e-commerce hay por cada estado (completado, cancelado, pendiente)?',
        '¿Cuál es el total de ingresos por categoría de producto en la tienda online?',
        '¿Cuántos clientes únicos hicieron pedidos en la plataforma de e-commerce?',
        '¿Cuántos pedidos fueron cancelados en la tienda online?',
        '¿Cuáles son las 5 categorías de productos con más pedidos vendidos?',
        '¿Cuántos pedidos hay por ciudad de origen del cliente en el e-commerce?',
        '¿Cuál es el total de ingresos por día de la semana en el retail?',
        '¿Cuáles son los motivos de cancelación de pedidos más frecuentes en la tienda?',
        '¿Cuáles son los 5 fabricantes con mayores ingresos por ventas en el e-commerce?',
        '¿Cuántos pedidos hay por género del cliente (masculino, femenino) en la tienda online?'
    ],
    'industry_fields': {
        'category',
        'customer_gender',
        'customer_id',
        'day_of_week',
        'geo.city',
        'geo.country',
        'manufacturer',
        'order_id',
        'status',
        'taxful_total_price',
        'total_quantity',
    },
    'dataset_files': ['ventas-ecommerce.log'],
    'capability': {
        'label': 'Ventas e-commerce',
        'index_pattern': 'ventas-ecommerce*',
        'operations': [
            "Men's Clothing",
            "Men's Shoes",
            "Men's Accessories",
            "Women's Clothing",
            "Women's Shoes",
            "Women's Accessories"
        ],
        'success_code': '',
        'fields': {
            'status': 'COMPLETED, RETURNED or CANCELLED',
            'cancel_reason': 'PAYMENT_FAILED, CUSTOMER_REQUEST or OUT_OF_STOCK; present ONLY on cancelled orders',
            'category': 'product category (see OPERATIONS for the exact names)',
            'manufacturer': 'brand / manufacturer',
            'customer_id': 'customer id (for unique/active customers)',
            'customer_gender': 'MALE or FEMALE',
            'total_quantity': 'items in the order',
            'taxful_total_price': 'order total (EUR); sum it for revenue',
            'geo.city': 'customer city',
            'geo.country': 'customer country code (GB, US, ...)',
            'day_of_week': 'Monday..Sunday',
        },
        'volume_field': 'order_id',
        'forecast_interval_minutes': 240,
        'forecast_horizon': 8,
        'forecasts': [
            {
                'name': 'ecommerce-orders-forecast',
                'feature_name': 'orders_volume',
                'aggregation_query': {
                    'orders_volume': {
                        'value_count': {
                            'field': 'order_id',
                        },
                    },
                },
                'description': 'Forecast de volumen de ordenes por intervalo',
            },
            {
                'name': 'ecommerce-revenue-forecast',
                'feature_name': 'revenue',
                'aggregation_query': {
                    'revenue': {
                        'sum': {
                            'field': 'taxful_total_price',
                        },
                    },
                },
                'description': 'Forecast de revenue (EUR) por intervalo',
            },
            {
                'name': 'ecommerce-customers-forecast',
                'feature_name': 'active_customers',
                'aggregation_query': {
                    'active_customers': {
                        'cardinality': {
                            'field': 'customer_id',
                        },
                    },
                },
                'description': 'Forecast de clientes activos unicos por intervalo',
                'history': 2000,
            }
        ],
    },
    'dashboard': {
        'title': 'Ventas e-commerce',
        'index_fields': [
            ('@timestamp', 'date'),
            ('order_id', 'keyword'),
            ('status', 'keyword'),
            ('cancel_reason', 'keyword'),
            ('category', 'keyword'),
            ('manufacturer', 'keyword'),
            ('customer_id', 'keyword'),
            ('customer_gender', 'keyword'),
            ('total_quantity', 'long'),
            ('taxful_total_price', 'double'),
            ('geo.city', 'keyword'),
            ('geo.country', 'keyword'),
            ('day_of_week', 'keyword')
        ],
        'panels': [
            {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## Ventas e-commerce — órdenes y revenue
Volumen de órdenes, revenue, cancelaciones, categorías, clientes y geo."""},
            {'type': 'controls', 'title': 'Filtros', 'controls': [
                {'field': 'status', 'label': 'Estado'}, {'field': 'category', 'label': 'Categoría'},
                {'field': 'customer_gender', 'label': 'Género'}, {'field': 'day_of_week', 'label': 'Día de la semana'}]},
            {'type': 'metric', 'title': 'Total de Pedidos', 'agg': 'count'},
            {'type': 'metric', 'title': 'Ingresos (EUR)', 'agg': 'sum', 'field': 'taxful_total_price', 'label': 'Ingresos (EUR)'},
            {'type': 'metric', 'title': 'Clientes Únicos', 'agg': 'cardinality', 'field': 'customer_id', 'label': 'Clientes Únicos'},
            {'type': 'metric', 'title': 'Pedidos Cancelados', 'agg': 'count', 'label': 'Pedidos Cancelados', 'query': 'status:CANCELLED'},
            {'type': 'area', 'title': 'Pedidos en el tiempo por categoría', 'metric': 'count', 'split': 'category', 'w': 48},
            {'type': 'line', 'title': 'Ingresos en el tiempo', 'metric': 'sum', 'field': 'taxful_total_price'},
            {'type': 'area', 'title': 'Éxitos vs Cancelados en el tiempo', 'metric': 'count', 'split': 'status'},
            {'type': 'bar', 'title': 'Motivos de Cancelación', 'field': 'cancel_reason', 'horizontal': True},
            {'type': 'table', 'title': 'Top Fabricantes por Ingresos', 'field': 'manufacturer', 'metric': 'sum', 'agg_field': 'taxful_total_price'},
            {'type': 'bar', 'title': 'Top Ciudades', 'field': 'geo.city', 'horizontal': True},
            {'type': 'bar', 'title': 'Top Países', 'field': 'geo.country', 'horizontal': True},
            {'type': 'table', 'title': 'Top Clientes por Pedidos', 'field': 'customer_id'},
        ],
    },
}
