#!/usr/bin/python
# -*- coding: utf-8 -*-

import datetime
import requests
import simplejson as json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.core.urlresolvers import reverse
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.conf import settings
from django.contrib.auth import login, authenticate

from wash.models import VerifyCode, WashUserProfile, WashType, IndexBanner
from wash.models import Basket, UserAddress, Address, Order, OrderDetail, OrderLog
from wash.models import Discount, MyDiscount, Advice, Company
from wash.forms import RegistForm
from CCPRestSDK import REST
from utils import gen_verify_code, adjacent_paginator
from utils.verify import Code
from wechat.views import code_get_openid
from wechat.models import WeProfile
from gather.celery import send_wechat_msg
from wechat_pay import UnifiedOrder_pub, JsApi_pub

WASH_URL = settings.WASH_URL
OAUTH_WASH_URL = settings.OAUTH_WASH_URL

INFO_LOG = logging.getLogger('info')


def auto_login(func):
    def wrapped(_request, *args, **kwargs):
        user = _request.user
        if not user.is_authenticated():
            status, open_id = code_get_openid(_request)
            INFO_LOG.info(status, open_id, _request.GET.get('next', ''))
            if status:
                if WeProfile.objects.filter(open_id=open_id).exists():
                    profile = WeProfile.objects.get(open_id=open_id)
                    user = profile.user
                    if WashUserProfile.user_valid(user):
                        user = authenticate(remote_user=user.username)
                        user.backend = 'django.contrib.auth.backends.ModelBackend'
                        login(_request, user)
                        return HttpResponseRedirect(_request.GET.get('next', '/wash/account/'))
                else:
                    kwargs['open_id'] = open_id
        return func(_request, *args, **kwargs)
        wrapped.__doc__ = func.__doc__
        wrapped.__name__ = func.__name__
    return wrapped


def oauth(request):
    redirect_uri = request.GET.get('redirect_uri')
    return HttpResponseRedirect(redirect_uri+"&code=123")


def index(request, template_name='wash/index.html'):
    img_list = IndexBanner.objects.filter(is_show=True).order_by("index")
    imgs, page_numbers = adjacent_paginator(img_list, page=request.GET.get('page', 1))
    return render(request, template_name, {
        'imgs': imgs,
        'page_numbers': page_numbers
    })


@auto_login
def account(request, template_name='wash/account.html'):
    user = request.user
    if user.is_authenticated():
        profile = WashUserProfile.objects.get(user=user)
    else:
        profile = None
    return render(request, template_name, {
        'profile': profile
    })


def verify_code_img(request):
    """ 更改验证码"""
    code = Code(request)
    code.type = 'word'
    return code.display()


def verify_code_check(request):
    """ 检测验证码"""
    code = Code(request)
    input_code = request.GET.get('code', '')
    return HttpResponse(json.dumps(code.check(input_code)))


@csrf_exempt
def verify_code(request):
    if request.is_ajax():
        phone = request.POST.get('phone', '')
        if len(phone) == 11:
            if WashUserProfile.objects.filter(phone=phone, is_phone_verified=True).exists():
                # 临时
                user = authenticate(remote_user=phone)
                user.backend = 'django.contrib.auth.backends.ModelBackend'
                login(request, user)
                return HttpResponse(json.dumps({"result": True, 'msg': 'login'}))
            else:
                rest = REST()
                if VerifyCode.objects.filter(phone=phone, is_expire=False).exists():
                    verify = VerifyCode.objects.get(phone=phone, is_expire=False)
                    if (datetime.datetime.today() - verify.created).seconds > settings.VERIFY_CODE_EXPIRE:
                        verify_code = gen_verify_code()
                        verify.is_expire = True
                        verify.save()
                        VerifyCode(phone=phone, code=verify_code).save()
                    else:
                        verify_code = verify.code
                else:
                    verify_code = gen_verify_code()
                    VerifyCode(phone=phone, code=verify_code).save()
                #result = rest.voice_verify(verify_code, phone)
                result = rest.sendTemplateSMS(phone, [verify_code, 2], 42221)['statusCode']
                result = True if result == '000000' else False
                return HttpResponse(json.dumps({'result': result}))
        return HttpResponse(json.dumps({"result": False, 'msg': u'手机号格式错误'}))
    return render(request)


@auto_login
def regist(request, form_class=RegistForm, template_name='wash/regist.html', open_id=None):
    if request.method == "POST":
        form = form_class(request, data=request.POST)
        if form.is_valid():
            form.login()
            return HttpResponseRedirect(request.POST.get('next', '/wash/'))
    else:
        form = form_class()
        next = request.GET.get('next', '/')
    return render(request, template_name, {
        'form': form,
        'open_id': open_id,
        'next': next,
    })


@auto_login
def show(request, template_name='wash/show.html'):
    wash_arr = get_show_info(request)
    if request.is_ajax():
        return HttpResponse(json.dumps({'status': True, 'result': wash_arr}))

    return render(request, template_name, {
        'wash_arr': wash_arr,
    })


@login_required(login_url=OAUTH_WASH_URL.format(next='/wash/user/order/'))
def user_order(request, template_name="wash/user_order.html"):
    profile = request.user.wash_profile

    company_discounts_dict = Discount.short_descs(request.user)
    order_list = Order.objects.filter(user=profile).order_by('-updated')
    order_id_arr = list(set([order.id for order in order_list]))
    order_detail_list = OrderDetail.objects.filter(order_id__in=order_id_arr)

    detail_dict = {}
    for detail in order_detail_list:
        d = {
            'id': detail.id,
            'count': detail.count,
            'price': detail.price,
            'photo': detail.photo,
            'belong': detail.get_belong_display(),
            'measure': detail.get_measure_display(),
            'name': detail.name,
            'short_desc': company_discounts_dict.get(detail.wash_type_id, '')
        }
        if detail.order_id in detail_dict:
            detail_dict[detail.order_id].append(d)
        else:
            detail_dict[detail.order_id] = [d]

    orders = []
    for order in order_list:
        o = {}
        o['id'] = order.id
        o['money'] = order.money
        o['created'] = order.created
        o['status'] = order.get_status_display()
        o['status_id'] = order.status
        o['detail'] = detail_dict.get(order.id, [])
        o['count'] = sum(d['count'] for d in detail_dict.get(order.id, []))
        orders.append(o)

    return render(request, template_name, {
        'orders': orders,
    })


@login_required(login_url=OAUTH_WASH_URL.format(next='/wash/user/order/detail/0/'))
def user_order_detail(request, order_id, template_name='wash/user_order_detail.html'):
    """
    用户交易流程
    """
    logs = OrderLog.objects.filter(order_id=order_id).order_by('-created')
    if Order.exists(order_id):
        order = Order.objects.get(pk=order_id)
    else:
        order = None
    return render(request, template_name, {
        'logs': logs,
        'order': order,
    })


@login_required(login_url=OAUTH_WASH_URL.format(next='/wash/user/order/cancel/0/'))
def user_order_cancel(request, order_id):
    """
    用户取消订单
    """
    Order.status_close(order_id)
    return HttpResponseRedirect(reverse("wash.views.user_order"))


def basket(request, template_name='wash/basket.html'):
    """
    购物车
    """
    wash_arr = basket_info(request)
    return render(request, template_name, {
        'wash_arr': wash_arr,
    })


@login_required(login_url=OAUTH_WASH_URL.format(next='/wash/order/'))
def order(request, template_name="wash/order.html"):
    """
    下单
    :param request:
    :return:
    """
    profile = request.user.wash_profile
    wash_list = basket_info(request)
    price_sum = reduce(lambda x, y: x+y, [wash['count']*wash['new_price']for wash in wash_list])
    wash_count = reduce(lambda x, y: x+y, [wash['count'] for wash in wash_list])
    if wash_count < 30:
        price_sum += 8

    # 合作账号优先使用提供给公司的、类型为全部的优惠
    discount = None
    if profile.is_company_user:
        discount = Discount.get_discount(1, company=profile.company)
    else:
        discount = Discount.get_discount(1)

    if discount is not None:
        if discount.discount_type == 1:
            price_sum -= int(discount.price)
        else:
            price_sum *= float(discount.price)

    if request.method == "POST":
        address_id = request.POST.get('address_id')
        service_time = request.POST.get('service_time')
        am_pm = request.POST.get('am_pm')
        mark = request.POST.get('mark')

        order = Order(user=profile, address_id=address_id, mark=mark,
                      money=price_sum, service_time=service_time,
                      am_pm=am_pm, verify_code=gen_verify_code())
        order.save()
        for wash in wash_list:
            OrderDetail(order=order, wash_type_id=wash['id'],
                        count=wash['count'], price=wash['new_price'],
                        photo=wash['photo'], measure=wash['measure_id'],
                        name=wash['name'], belong=wash['belong_id']).save()
        Basket.submit(request.session.session_key)
        OrderLog.create(order.id, 1)
        data = {
            'first': {'value': u'您好，您已下单成功。', 'color': '#173177'},
            'keyword1': {'value': order.id, 'color': '#173177'},
            'keyword2': {'value': u'创建成功', 'color': '#173177'},
            'keyword3': {'value': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'color': '#173177'},
            'remark': {'value': u'请耐心等待客服与您确认', 'color': '#173177'},
        }
        send_wechat_msg(request.user, 'order_create', order.id, data)
        return HttpResponseRedirect(reverse('wash.views.user_order'))
    else:
        choose = request.GET.get('choose', None)
        address = UserAddress.get_default(profile, choose)
    return render(request, template_name, {
        'address': address,
        'profile': profile,
        'wash_list': wash_list,
        'price_sum': price_sum,
        'today': datetime.datetime.now(),
        'discount': discount,
    })


@csrf_exempt
def basket_update(request):
    if request.method == "POST" and request.is_ajax():
        wash_id = request.POST.get('key')
        flag = request.POST.get('flag')
        sessionid = request.session.session_key
        if Basket.is_sessionid_exist(sessionid):
            if Basket.is_wash_exist(sessionid, wash_id):
                Basket.update(sessionid, wash_id, flag)
            else:
                Basket(sessionid=sessionid, wash_id=wash_id, count=1).save()
        else:
            Basket(sessionid=sessionid, wash_id=wash_id, count=1).save()
    return HttpResponse(json.dumps(True))


def get_show_info(request):
    # 洗刷清单页
    belong = request.GET.get('belong', '2')
    user = request.user

    param = {
        'belong': belong,
        'is_for_company': False
    }

    if user.is_authenticated():
        try:
            if user.wash_profile.is_company_user:
                param['is_for_company'] = True
        except:
            pass

    washes = WashType.objects.filter(**param)

    return basket_info(request, washes)


def basket_info(request, washes=None):
    # 组装预购数据, 同时可用来反馈用户选择了哪些商品
    sessionid = request.session.session_key
    basket_list = Basket.get_list(sessionid)
    company_discounts_dict = Discount.short_descs(request.user)
    if washes is None:
        washes = WashType.objects.filter(id__in=basket_list.keys())

    wash_arr = []
    for wash in washes:
        w = {}
        w['id'] = wash.id
        w['name'] = wash.name
        w['new_price'] = wash.new_price
        w['old_price'] = wash.old_price
        w['measure'] = wash.get_measure_display()
        w['belong'] = wash.get_belong_display()
        w['measure_id'] = wash.measure
        w['belong_id'] = wash.belong
        w['is_for_company'] = wash.is_for_company
        w['short_desc'] = company_discounts_dict.get(wash.id, '')
        w['photo'] = wash.get_photo_url()
        w['count'] = basket_list.get(wash.id, 0)
        wash_arr.append(w)
    return wash_arr


@login_required(login_url=OAUTH_WASH_URL.format(next='/wash/user/address/'))
def user_address(request, template_name="wash/address.html"):
    place = request.GET.get('place', 'account')
    addresses = UserAddress.objects.filter(user=request.user.wash_profile)
    return render(request, template_name, {
        'addresses': addresses,
        'place': place,
    })


@login_required(login_url=OAUTH_WASH_URL.format(next='/wash/address/select/'))
def user_address_select(request, template_name="wash/address_select.html"):
    addresses = UserAddress.objects.filter(user=request.user.wash_profile)
    return render(request, template_name, {
        'addresses': addresses,
    })


@login_required(login_url=OAUTH_WASH_URL.format(next='/wash/address/add/'))
def user_address_add(request, template_name="wash/address_add.html"):
    if request.method == 'POST':
        country_id = request.POST.get('country', '')
        country = Address.get_name(country_id)
        street = request.POST.get('street', '')
        mark = request.POST.get('mark', '')
        name = request.POST.get('name', '')
        phone = request.POST.get('phone', '')
        place = request.POST.get('place', 'account')

        if UserAddress.has_default(request.user.wash_profile):
            is_default = False
        else:
            is_default = True
        UserAddress(user=request.user.wash_profile,
                    province=u'北京',
                    city=u'北京',
                    name=name,
                    phone=phone,
                    country=country,
                    street=street,
                    mark=mark.strip(),
                    is_default=is_default).save()
        if place == 'account':
            redirect = 'wash.views.user_address'
        else:
            redirect = 'wash.views.user_address_select'
        return HttpResponseRedirect(reverse(redirect))
    else:
        countrys = Address.objects.filter(pid=1101)
        streets = Address.objects.filter(pid=110101)
        place = request.GET.get('place', 'account')
    return render(request, template_name, {
        'countrys': countrys,
        'streets': streets,
        'place': place,
    })


def address_street(request):
    # ajax获取街道信息
    if request.is_ajax():
        pid = request.GET.get('pid', 0)
        streets = Address.objects.filter(pid=pid)
        street_arr = []
        for street in streets:
            street_arr.append(street.name)
        return HttpResponse(json.dumps({'result': True, 'info': street_arr}))


@login_required(login_url=OAUTH_WASH_URL.format(next='/wash/user/address/'))
def user_address_update(request, address_id, template_name="wash/address_update.html"):
    try:
        address = UserAddress.objects.get(id=address_id)
    except UserAddress.DoesNotExist:
        return HttpResponseRedirect(reverse('wash.views.user_address'))

    if request.method == "POST":
        place = request.POST.get('place', 'account')
        country_id = request.POST.get('country', '')
        country = Address.get_name(country_id)
        street = request.POST.get('street', '')
        mark = request.POST.get('mark', '')
        name = request.POST.get('name', '')
        phone = request.POST.get('phone', '')
        is_default = request.POST.get('is_default', '0')
        if is_default == '1':
            profile = request.user.wash_profile
            if UserAddress.has_default(profile):
                default = UserAddress.get_default(profile)
                default.is_default = 0
                default.save()
            address.is_default = is_default
        address.country = country
        address.street = street
        address.mark = mark
        address.phone = phone
        address.name = name
        address.save()
        return HttpResponseRedirect('%s?place=%s' % (reverse('wash.views.user_address'), place))
    else:
        place = request.GET.get('place', 'account')
        countrys = Address.objects.filter(pid=1101)
        streets = Address.objects.filter(pid=Address.get_id(address.country))

    return render(request, template_name, {
        'address': address,
        'place': place,
        'countrys': countrys,
        'streets': streets,
    })


@login_required(login_url=OAUTH_WASH_URL.format(next='/wash/user/discount/'))
def user_discount(request, template_name="wash/user_discount.html"):
    user = request.user.wash_profile
    discounts = MyDiscount.objects.filter(phone=user.phone)
    return render(request, template_name, {
        'discounts': discounts
    })


def discount_get(request):
    if request.method == "POST":
        phone = request.POST.get('phone', '')
        did = request.POST.get('did', '')
        if Discount.is_exists(did):
            if MyDiscount.has(phone, did):
                messages.error(request, u'已领取过了')
            else:
                MyDiscount(phone=phone, discount_id=did).save()
                messages.info(request, u'领取成功,进入公众号查看')
        else:
            messages.error(request, u'不存在')


def advice(request, template_name='wash/advice.html'):
    """
    意见建议
    :param request:
    :param template_name:
    :return:
    """
    if request.method == 'POST':
        user = request.user
        content = request.POST.get('content', '')
        if content:
            if user.is_authenticated():
                Advice(user=user.wash_profile, content=content).save()
            else:
                Advice(content=content).save()
            messages.info(request, '建议成功')
        else:
            messages.error(request, '内容为空')

    return render(request, template_name)


@login_required(login_url=OAUTH_WASH_URL.format(next='/wash/verify/company/'))
def verify_company(request, template_name='wash/verify_company.html'):
    user = request.user
    profile = user.wash_profile
    if request.method == 'POST':
        short = request.POST.get('short', '')
        if Company.exists(short):
            company = Company.objects.get(short=short)
            WashUserProfile.objects.filter(user=user).update(is_company_user=True, company=company)
            return HttpResponseRedirect(reverse('wash.views.account'))
        else:
            messages.error(request, u'验证码错误')
    else:
        if request.GET.get('rebind', ''):
            profile = None
    return render(request, template_name, {
        'profile': profile,
    })


@login_required(login_url=OAUTH_WASH_URL.format(next='/wash/user/order/'))
def wechat_pay(request, order_id, template_name='wash/pay.html'):
    order = Order.objects.get(pk=order_id)
    weprofile = WeProfile.objects.get(user=request.user)
    pay = UnifiedOrder_pub()
    h5_pay = JsApi_pub()
    pay.setParameter("out_trade_no", datetime.datetime.now().strftime('%Y%m%d%H%M%S%f'))
    pay.setParameter("body", "test")
    pay.setParameter("total_fee", str(order.money*100))
    pay.setParameter("notify_url", "1")
    pay.setParameter("trade_type", "JSAPI")
    pay.setParameter("openid", 'oXP2qt4NT-izUpr_B86wbViypiqI')
    #preypay_id = pay.getPrepayId()
    preypay_id = "wx201511091542253d0517f9b80856928950"

    h5_pay.setPrepayId(preypay_id)
    parameters = h5_pay.getParameters()
    print parameters
    return render(request, template_name, parameters)




