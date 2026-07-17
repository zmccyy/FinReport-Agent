import requests, json

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'X-Requested-With': 'XMLHttpRequest',
    'Origin': 'http://www.cninfo.com.cn',
    'Referer': 'http://www.cninfo.com.cn/',
}

# 查询股票 orgId
url = 'http://www.cninfo.com.cn/new/information/topSearch/detailOfQuery'
data = {
    'keyWord': '000001',
    'maxSecNum': 10,
    'maxListNum': 5,
}
r = requests.post(url, data=data, headers=headers, timeout=15)
print('=== 000001 ===')
print(r.text[:800])
print()

data['keyWord'] = '600519'
r = requests.post(url, data=data, headers=headers, timeout=15)
print('=== 600519 ===')
print(r.text[:800])
print()

data['keyWord'] = '300750'
r = requests.post(url, data=data, headers=headers, timeout=15)
print('=== 300750 ===')
print(r.text[:800])
