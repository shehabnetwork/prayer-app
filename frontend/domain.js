export const PRAYERS = Object.freeze([
  {key:'fajr', name:'الفجر', caption:'بداية يوم جميل', theme:'fajr', before:'fajr_before'},
  {key:'dhuhr', name:'الظهر', caption:'نور وسط النهار', theme:'dhuhr', before:'dhuhr_before', after:'dhuhr_after'},
  {key:'asr', name:'العصر', caption:'خطوة حلوة في يومك', theme:'asr'},
  {key:'maghrib', name:'المغرب', caption:'مع ألوان الغروب', theme:'maghrib', after:'maghrib_after'},
  {key:'isha', name:'العشاء', caption:'ختام يومك بهدوء', theme:'isha', after:'isha_after'},
]);
export const STATES = Object.freeze([
  {label:'لم أصلّ بعد', image:'child-waiting.svg', next:'صلّيت في البيت', className:'waiting'},
  {label:'صلّيت في البيت', image:'child-home.png', next:'صلّيت في المسجد', className:'home'},
  {label:'صلّيت في المسجد', image:'child-mosque.png', next:'لم أصلّ بعد', className:'mosque'},
]);
export const WAITING_STATE = Object.freeze({label:'في انتظار الصلاة',image:'child-waiting.svg',className:'not-due'});
export const DAILY_MEDAL_TARGETS = Object.freeze({gold:PRAYERS.length*27,silver:7});
export const SILVER_TARGETS = Object.freeze({fajr:1,dhuhr:3,asr:0,maghrib:1,isha:2});
export const nextState = state => (state + 1) % 3;
export function eligiblePrayerKeys(day,now=new Date()) {
  const schedule=day?.schedule;
  if(schedule==null)return PRAYERS.map(p=>p.key);
  if(!schedule.today)return null;
  if(day.date<schedule.today)return PRAYERS.map(p=>p.key);
  if(day.date>schedule.today)return [];
  if(!schedule.available||!schedule.times)return null;
  const instant=now instanceof Date?now.getTime():new Date(now).getTime();
  return PRAYERS.filter(p=>Number.isFinite(new Date(schedule.times[p.key]).getTime())&&new Date(schedule.times[p.key]).getTime()<=instant).map(p=>p.key);
}
export function dailyStats(day,now=new Date()) {
  const legacy=day?.schedule==null;
  const eligible=eligiblePrayerKeys(day,now);
  if(eligible===null)return {completed:null,total:null,home:null,mosque:null,sunnah_rakahs:null,witr:null,gold_medals:null,silver_medals:null,gold_target:null,silver_target:null,percent:null};
  const keys=eligible??[];
  const values=keys.map(key=>day.prayers[key]);
  const completed=values.filter(value=>value>0).length;
  const sunnahPrayer={fajr_before:'fajr',dhuhr_before:'dhuhr',dhuhr_after:'dhuhr',maghrib_after:'maghrib',isha_after:'isha',witr:'isha'};
  const sunnah_rakahs=Object.entries(day.sunnah).filter(([key])=>key!=='witr'&&keys.includes(sunnahPrayer[key])).reduce((sum,[,value])=>sum+value,0);
  const silver_medals=(keys.includes('dhuhr')?(day.dhuhr_before_blocks||[]).filter(Boolean).length:0)+Object.entries(day.sunnah).filter(([key,value])=>!['dhuhr_before','witr'].includes(key)&&keys.includes(sunnahPrayer[key])&&value>0).length+Number(keys.includes('isha')&&Boolean(day.sunnah.witr));
  const gold_medals=values.reduce((sum,value)=>sum+(value===1?1:value===2?27:0),0);
  const total=keys.length;
  const gold_target=keys.length*27;
  const silver_target=keys.reduce((sum,key)=>sum+SILVER_TARGETS[key],0);
  const result={completed,total,home:values.filter(value=>value===1).length,mosque:values.filter(value=>value===2).length,sunnah_rakahs,witr:keys.includes('isha')&&day.sunnah.witr,gold_medals,silver_medals,percent:total?Math.round(completed/total*100):0};
  return legacy?result:{...result,gold_target,silver_target};
}
export function localDate(date=new Date()) {
  return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
}
export function dateInTimezone(timezone,date=new Date()) {
  if(!timezone)return localDate(date);
  try{return new Intl.DateTimeFormat('en-CA',{timeZone:timezone,year:'numeric',month:'2-digit',day:'2-digit'}).format(date);}catch{return localDate(date);}
}
export function shiftDate(date,days) {
  const d=new Date(`${date}T12:00:00`); d.setDate(d.getDate()+days); return localDate(d);
}
export function achievementRange(period='last30',today=localDate()) {
  const current=new Date(`${today}T12:00:00`);
  if(period==='week') {
    const from=shiftDate(today,-((current.getDay()+1)%7));
    return {from,to:shiftDate(from,6)};
  }
  if(period==='month') {
    return {
      from:localDate(new Date(current.getFullYear(),current.getMonth(),1,12)),
      to:localDate(new Date(current.getFullYear(),current.getMonth()+1,0,12)),
    };
  }
  return {from:shiftDate(today,-29),to:today};
}
export function emptyDay(date) {
  return {date,version:0,prayers:Object.fromEntries(PRAYERS.map(p=>[p.key,0])),sunnah:{fajr_before:0,dhuhr_before:0,dhuhr_after:0,maghrib_after:0,isha_after:0,witr:false},dhuhr_before_blocks:[false,false]};
}
export function toggleDhuhrBlocks(blocks,index) {
  const next=[...blocks]; next[index]=!next[index]; return {blocks:next,total:next.filter(Boolean).length*2};
}
