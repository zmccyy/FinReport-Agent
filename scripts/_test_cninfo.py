import requests

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'X-Requested-With': 'XMLHttpRequest',
    'Origin': 'http://www.cninfo.com.cn',
    'Referer': 'http://www.cninfo.com.cn/new/disclosure/stock?stockCode=000001&orgId=gssz0000001',
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
}

url = 'http://www.cninfo.com.cn/new/hisAnnouncement/query'
data = {
    'stock': 'sz,000001',
    'tabName': 'fulltext',
    'pageSize': '10',
    'pageNum': '1',
    'column': 'szse',
    'category': 'category_ndbg_szsh;',
    'seDate': '2023-01-01~2024-12-31',
}

r = requests.post(url, data=data, headers=headers, timeout=15)
print('status:', r.status_code)
print('content[:1000]:', r.text[:1000])
