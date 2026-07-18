import requests
import os
import time

base = r'E:\项目\FinReport Agent\data\sample_reports'
os.makedirs(base, exist_ok=True)

stocks = [
    ('000001', '平安银行', 'sz'),
    ('600519', '贵州茅台', 'sh'),
    ('300750', '宁德时代', 'sz'),
]

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Origin': 'http://www.cninfo.com.cn',
    'Referer': 'http://www.cninfo.com.cn/',
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
}

url = 'http://www.cninfo.com.cn/new/hisAnnouncement/query'

for code, name, market in stocks:
    print(f'=== 查询 {name} ({code}) ===')
    data = {
        'stock': f'{market},{code}',
        'tabName': 'fulltext',
        'pageSize': 30,
        'pageNum': 1,
        'column': 'szse' if market == 'sz' else 'sse',
        'plate': '',
        'stockType': '',
        'category': 'category_ndbg_szsh;',
        'seDate': '2023-01-01~2024-12-31',
        'searchkey': '',
        'secCode': '',
        'secName': '',
    }
    try:
        r = requests.post(url, data=data, headers=headers, timeout=15)
        r.raise_for_status()
        result = r.json()
        announcements = result.get('announcements', [])
        print(f'  找到 {len(announcements)} 条公告')
        for ann in announcements[:5]:
            title = ann.get('announcementTitle', '')
            print(f'    - {title}')
    except Exception as e:
        print(f'  查询失败: {e}')
    time.sleep(1)
