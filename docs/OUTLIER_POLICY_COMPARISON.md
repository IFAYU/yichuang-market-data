# Outlier policy comparison  (release c3838b0ba4c4, TWSE 2026-10-05, TPEx 2026-10-06)

## 半導體業 (24): 207 companies, 168 with a positive official P/E

| method | n | excluded | P25 | median | mean | P75 | min | max |
|---|---|---|---|---|---|---|---|---|
| A RAW | 168 | 0 | 19.0 | 32.2 | 67.1 | 61.7 | 4.0 | 1165.0 |
| B CURRENT_ROBUST (k=3, n>=8) | 156 | 12 | 18.8 | 29.4 | 42.4 | 55.4 | 4.0 | 186.6 |
| C LEGACY (<200 then k=1.5) | 148 | 20 | 18.7 | 28.0 | 36.8 | 50.1 | 4.0 | 111.3 |

- removed ONLY by C (legacy): 8  7828 創新服務 (186.6), 4991 環宇-KY (157.3), 7856 漢測 (155.3), 3016 嘉晶 (151.2), 4971 IET-KY (145.9), 5274 信驊 (126.6), 6423 億而得 (124.5), 6223 旺矽 (121.4)
- removed ONLY by B (current): 0  —
- removed by BOTH: 12  8040 九暘 (1165.0), 6182 合晶 (556.5), 6643 M31 (534.7), 6451 訊芯-KY (396.3), 3532 台勝科 (361.1), 6895 宏碩系統 (308.8), 2401 凌陽 (266.5), 6708 天擎 (235.2), 3443 創意 (220.5), 3178 公準 (214.4), 3189 景碩 (203.2), 7772 耀穎 (191.1)

- legacy fences after the <200 cap: [-36.8, 111.4]
- current fences: [-109.0, 189.6]

## 電子零組件業 (28): 210 companies, 168 with a positive official P/E

| method | n | excluded | P25 | median | mean | P75 | min | max |
|---|---|---|---|---|---|---|---|---|
| A RAW | 168 | 0 | 19.8 | 31.4 | 53.1 | 58.1 | 3.7 | 398.1 |
| B CURRENT_ROBUST (k=3, n>=8) | 159 | 9 | 19.1 | 29.4 | 41.6 | 52.4 | 3.7 | 162.8 |
| C LEGACY (<200 then k=1.5) | 149 | 19 | 18.4 | 26.5 | 35.3 | 45.3 | 3.7 | 103.6 |

- removed ONLY by C (legacy): 10  2483 百容 (162.8), 3653 健策 (162.6), 4542 科嶠 (155.1), 5498 凱崴 (154.0), 5475 德宏 (140.3), 6108 競國 (128.2), 3376 新日興 (118.3), 6213 聯茂 (116.4), 2328 廣宇 (112.3), 6835 圓裕 (106.7)
- removed ONLY by B (current): 0  —
- removed by BOTH: 9  8121 越峰 (398.1), 5309 系統電 (395.3), 6715 嘉基 (316.1), 7795 長廣 (247.3), 3550 聯穎 (209.2), 3548 兆利 (201.3), 5464 霖宏 (193.4), 8046 南電 (175.7), 6538 倉和 (173.6)

- legacy fences after the <200 cap: [-30.7, 103.6]
- current fences: [-95.1, 173.0]

## 生技醫療業 (22): 159 companies, 101 with a positive official P/E

| method | n | excluded | P25 | median | mean | P75 | min | max |
|---|---|---|---|---|---|---|---|---|
| A RAW | 101 | 0 | 12.3 | 15.9 | 42.4 | 23.7 | 4.7 | 560.7 |
| B CURRENT_ROBUST (k=3, n>=8) | 90 | 11 | 12.0 | 14.7 | 18.2 | 20.5 | 4.7 | 55.1 |
| C LEGACY (<200 then k=1.5) | 82 | 19 | 11.7 | 14.3 | 15.4 | 17.9 | 4.7 | 34.7 |

- removed ONLY by C (legacy): 8  1784 訊聯 (55.1), 6615 慧智 (50.5), 1813 寶利徠 (48.7), 6649 台生材 (47.7), 6620 漢達 (46.2), 6861 睿生光電 (45.6), 4157 太景*-KY (38.5), 6612 奈米醫材 (36.9)
- removed ONLY by B (current): 0  —
- removed by BOTH: 11  6576 逸達 (560.7), 4743 合一 (370.0), 1789 神隆 (370.0), 4109 加捷生醫 (363.3), 6762 達亞 (312.0), 7814 海昌生技 (207.1), 6796 晉弘 (158.3), 4119 旭富 (108.6), 6841 長佳智能 (83.7), 6446 藥華藥 (58.3), 4160 訊聯基因 (57.9)

- legacy fences after the <200 cap: [-2.0, 35.7]
- current fences: [-21.8, 57.8]

## 食品工業 (02): 33 companies, 28 with a positive official P/E

| method | n | excluded | P25 | median | mean | P75 | min | max |
|---|---|---|---|---|---|---|---|---|
| A RAW | 28 | 0 | 12.6 | 16.5 | 28.1 | 21.0 | 10.2 | 156.4 |
| B CURRENT_ROBUST (k=3, n>=8) | 24 | 4 | 12.2 | 15.6 | 16.4 | 17.8 | 10.2 | 30.4 |
| C LEGACY (<200 then k=1.5) | 24 | 4 | 12.2 | 15.6 | 16.4 | 17.8 | 10.2 | 30.4 |

- removed ONLY by C (legacy): 0  —
- removed ONLY by B (current): 0  —
- removed by BOTH: 4  1220 台榮 (156.4), 1225 福懋油 (116.0), 1796 金穎生技 (72.2), 1702 南僑 (49.7)

- legacy fences after the <200 cap: [0.0, 33.5]
- current fences: [-12.5, 46.1]

## 建材營造業 (14): 88 companies, 73 with a positive official P/E

| method | n | excluded | P25 | median | mean | P75 | min | max |
|---|---|---|---|---|---|---|---|---|
| A RAW | 73 | 0 | 7.3 | 10.1 | 20.7 | 15.9 | 2.8 | 331.0 |
| B CURRENT_ROBUST (k=3, n>=8) | 66 | 7 | 7.1 | 9.8 | 11.9 | 13.5 | 2.8 | 41.0 |
| C LEGACY (<200 then k=1.5) | 62 | 11 | 6.9 | 9.4 | 10.4 | 12.7 | 2.8 | 27.2 |

- removed ONLY by C (legacy): 4  2524 京城 (41.0), 2545 皇翔 (35.8), 2530 華建 (30.1), 3266 昇陽 (29.0)
- removed ONLY by B (current): 0  —
- removed by BOTH: 7  2534 宏盛 (331.0), 1805 寶徠 (83.0), 2547 日勝生 (80.5), 5514 三豐 (62.7), 5531 鄉林 (60.8), 5543 桓鼎-KY (58.5), 5512 力麒 (53.5)

- legacy fences after the <200 cap: [-5.4, 28.3]
- current fences: [-18.7, 42.0]

## 金融保險業 (17): 40 companies, 40 with a positive official P/E

| method | n | excluded | P25 | median | mean | P75 | min | max |
|---|---|---|---|---|---|---|---|---|
| A RAW | 40 | 0 | 7.7 | 12.8 | 11.5 | 15.8 | 2.8 | 19.3 |
| B CURRENT_ROBUST (k=3, n>=8) | 40 | 0 | 7.7 | 12.8 | 11.5 | 15.8 | 2.8 | 19.3 |
| C LEGACY (<200 then k=1.5) | 40 | 0 | 7.7 | 12.8 | 11.5 | 15.8 | 2.8 | 19.3 |

- removed ONLY by C (legacy): 0  —
- removed ONLY by B (current): 0  —
- removed by BOTH: 0  —

- legacy fences after the <200 cap: [-4.4, 27.9]
- current fences: [-16.6, 40.0]

## 文化創意業 (32): 26 companies, 16 with a positive official P/E

| method | n | excluded | P25 | median | mean | P75 | min | max |
|---|---|---|---|---|---|---|---|---|
| A RAW | 16 | 0 | 11.7 | 16.2 | 25.4 | 34.4 | 4.0 | 90.9 |
| B CURRENT_ROBUST (k=3, n>=8) | 16 | 0 | 11.7 | 16.2 | 25.4 | 34.4 | 4.0 | 90.9 |
| C LEGACY (<200 then k=1.5) | 15 | 1 | 11.4 | 14.5 | 21.1 | 29.7 | 4.0 | 48.2 |

- removed ONLY by C (legacy): 1  6542 隆中 (90.9)
- removed ONLY by B (current): 0  —
- removed by BOTH: 0  —

- legacy fences after the <200 cap: [-22.3, 68.4]
- current fences: [-56.3, 102.4]
