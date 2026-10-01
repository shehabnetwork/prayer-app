import test from 'node:test';
import assert from 'node:assert/strict';
import {PRAYERS,nextState,dailyStats,emptyDay,shiftDate,toggleDhuhrBlocks,achievementRange,DAILY_MEDAL_TARGETS,eligiblePrayerKeys,dateInTimezone} from '../frontend/domain.js';
test('only five obligatory prayers and no Witr in denominator',()=>{
 const day=emptyDay('2026-09-30'); day.prayers.fajr=1;day.prayers.dhuhr=2;day.prayers.isha=1; day.sunnah.witr=true;day.sunnah.dhuhr_before=4;
 assert.equal(PRAYERS.length,5); assert.deepEqual(dailyStats(day),{completed:3,total:5,home:2,mosque:1,sunnah_rakahs:4,witr:true,gold_medals:29,silver_medals:1,percent:60});
});

test('current-day stats are zero before the first prayer and unlock on the exact boundary',()=>{
 const day=emptyDay('2026-10-01');
 day.schedule={today:'2026-10-01',available:true,times:{
  fajr:'2026-10-01T05:00:00+03:00',dhuhr:'2026-10-01T12:00:00+03:00',
  asr:'2026-10-01T15:35:00+03:00',maghrib:'2026-10-01T18:00:00+03:00',isha:'2026-10-01T19:00:00+03:00'}};
 day.prayers.fajr=2;
 const before=new Date('2026-10-01T01:59:59.999Z');
 const exact=new Date('2026-10-01T02:00:00.000Z');
 assert.deepEqual(eligiblePrayerKeys(day,before),[]);
 assert.equal(dailyStats(day,before).total,0);
 assert.equal(dailyStats(day,before).percent,0);
 assert.deepEqual(eligiblePrayerKeys(day,exact),['fajr']);
 assert.equal(dailyStats(day,exact).percent,100);
});

test('schedule timestamps and city dates do not depend on device timezone or DST',()=>{
 const day=emptyDay('2026-10-25');
 day.schedule={today:'2026-10-25',available:true,times:{
  fajr:'2026-10-25T05:00:00+03:00',dhuhr:'2026-10-25T12:00:00+03:00',
  asr:'2026-10-25T15:00:00+03:00',maghrib:'2026-10-25T18:00:00+03:00',isha:'2026-10-25T19:00:00+03:00'}};
 const now=new Date('2026-10-25T02:00:00.000Z');
 assert.deepEqual(eligiblePrayerKeys(day,now),['fajr']);
 assert.equal(dateInTimezone('Asia/Riyadh',new Date('2026-10-01T21:30:00Z')),'2026-10-02');
 assert.equal(dateInTimezone('Africa/Cairo',new Date('2026-10-25T20:30:00Z')),'2026-10-25');
});
test('daily medals count active controls exactly once and reverse cleanly',()=>{
 const day=emptyDay('2026-09-30');
 day.prayers.fajr=1; day.prayers.dhuhr=2;
 day.sunnah.fajr_before=2; day.sunnah.dhuhr_before=4; day.sunnah.dhuhr_after=2; day.dhuhr_before_blocks=[true,true];
 day.sunnah.maghrib_after=2; day.sunnah.isha_after=2; day.sunnah.witr=true;
 assert.equal(dailyStats(day).gold_medals,28);
 assert.equal(dailyStats(day).silver_medals,7);
 day.prayers.fajr=0; day.sunnah.witr=false; day.dhuhr_before_blocks=[true,false];
 assert.deepEqual({gold:dailyStats(day).gold_medals,silver:dailyStats(day).silver_medals},{gold:27,silver:5});
});
test('one character cycles waiting -> home -> mosque -> waiting',()=>assert.deepEqual([nextState(0),nextState(1),nextState(2)],[1,2,0]));
test('Dhuhr two blocks independently represent 0, 2 or 4 rakahs',()=>{
 assert.deepEqual(toggleDhuhrBlocks([false,false],1),{blocks:[false,true],total:2});
 assert.deepEqual(toggleDhuhrBlocks([false,true],0),{blocks:[true,true],total:4});
 assert.deepEqual(toggleDhuhrBlocks([true,true],1),{blocks:[true,false],total:2});
 assert.deepEqual(toggleDhuhrBlocks([true,false],0),{blocks:[false,false],total:0});
});
test('history navigation crosses month/year and leap day',()=>{
 assert.equal(shiftDate('2026-12-31',1),'2027-01-01');assert.equal(shiftDate('2024-03-01',-1),'2024-02-29');
});
test('achievement ranges use Saturday-Friday weeks, calendar months, and inclusive rolling 30 days',()=>{
 assert.deepEqual(achievementRange('week','2026-10-01'),{from:'2026-09-26',to:'2026-10-02'});
 assert.deepEqual(achievementRange('week','2026-10-02'),{from:'2026-09-26',to:'2026-10-02'});
 assert.deepEqual(achievementRange('week','2026-10-03'),{from:'2026-10-03',to:'2026-10-09'});
 assert.deepEqual(achievementRange('month','2024-02-29'),{from:'2024-02-01',to:'2024-02-29'});
 assert.deepEqual(achievementRange('month','2026-01-01'),{from:'2026-01-01',to:'2026-01-31'});
 assert.deepEqual(achievementRange('last30','2026-03-01'),{from:'2026-01-31',to:'2026-03-01'});
 assert.deepEqual(achievementRange('last30','2024-03-01'),{from:'2024-02-01',to:'2024-03-01'});
});
test('daily medal targets are the fixed 135 gold and 7 silver maximums',()=>{
 assert.deepEqual(DAILY_MEDAL_TARGETS,{gold:135,silver:7});
});
