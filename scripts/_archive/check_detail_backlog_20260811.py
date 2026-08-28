import sqlite3
c = sqlite3.connect('data/hangzhou_agent.sqlite3')
q = lambda s: c.execute(s).fetchone()[0]
print('=== 真正需要详情的存量 ===')
print('active 无JD        :', q("select count(*) from jobs where retention_status='active' and trim(coalesce(job_description,''))='' and job_invalid=0"))
print('active 活跃度空    :', q("select count(*) from jobs where retention_status='active' and trim(coalesce(recruiter_active_status,''))='' and job_invalid=0"))
print('active raw缺detail :', q("select count(*) from jobs where retention_status='active' and job_invalid=0 and raw_json not like '%\"detail\"%'"))
print('sample 无JD        :', q("select count(*) from jobs where retention_status='sample' and trim(coalesce(job_description,''))='' and job_invalid=0"))
print()
print('=== active 活跃度分布 ===')
for r in c.execute("select recruiter_active_status,count(*) from jobs where retention_status='active' group by 1 order by 2 desc limit 10"):
    print(f'  {r[0] or "(空)":14s} {r[1]}')
print()
print('=== 活跃度陈旧程度（detail_fetched_at 有值的才知道何时抓的）===')
for r in c.execute("select substr(detail_fetched_at,1,10) d,count(*) from jobs where trim(coalesce(detail_fetched_at,''))<>'' group by 1 order by 1 desc limit 5"):
    print(f'  {r[0]} {r[1]}')
