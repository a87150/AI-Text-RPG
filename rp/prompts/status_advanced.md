正文之后另起一行，输出这一轮的状态，用 JSON：

<StatusBlock>{"present":["甲","我"],"away":["丙"],"location":"某酒馆","time":"深夜","relations":"戒备","items":["湿伞","铜钥匙"],"goal":"等他先开口","facts":["钥匙到了甲手里"]}</StatusBlock>

字段：present（这一幕在场的人）、away（已经离开的人）、location（地点）、time（故事内时间）、
relations（双方当前关系）、items（关键物品数组）、goal（这个角色此刻想达成的事）、
facts（这一轮新定下、之后不能反悔的事实数组，没有就留空数组）。
角色进出场景时必须更新 present / away，不要重复已经记过的事实。
这一块在正文之外，正文里不要出现 JSON。
