import { describe, expect, it } from "vitest";
import { EMPTY_FILTER_INTENT } from "./taskFilterIntent";
import { groundedRows, isGroundedExecution } from "./groundedAsk";
import { groundedSeries } from "@/components/tasks/GroundedChart";
import type { GroundedAskExecution } from "@/types";
const execution: GroundedAskExecution={recognized:true,kind:"students",explanation:"ok",data:{columns:["student_id"],rows:[["002"],["001"]]},selection:{kind:"students",ids:["002","001"]},chart:null};
describe("grounded data contract",()=>{
 it("selects full rows by stable keys in query order",()=>{
  const rows=[{id:"001",name:"Alex",score:4},{id:"002",name:"Maya",score:8}];
  expect(groundedRows(rows,{...EMPTY_FILTER_INTENT,execution},"students",r=>r.id)).toEqual([rows[1],rows[0]]);
 });
 it("an empty match set does not fall back to all students",()=>{
  expect(groundedRows([{id:"001"}],{...EMPTY_FILTER_INTENT,execution:{...execution,selection:{kind:"students",ids:[]}}},"students",r=>r.id)).toEqual([]);
 });
 it("keeps missing values attached to the correct question label",()=>{
  expect(groundedSeries({type:"bar",x:["Q1","Q2","Q3"],y:[5,null,7]})).toEqual([{label:"Q1",value:5},{label:"Q2",value:null},{label:"Q3",value:7}]);
 });
 it("rejects malformed, non-finite and misaligned result rows",()=>{
  expect(isGroundedExecution(execution)).toBe(true);
  expect(isGroundedExecution({...execution,data:{columns:["a","a"],rows:[[1,2]]}})).toBe(false);
  expect(isGroundedExecution({...execution,data:{columns:["a"],rows:[[NaN]]}})).toBe(false);
  expect(isGroundedExecution({...execution,data:{columns:["a"],rows:[[1,2]]}})).toBe(false);
 });
 it("rejects mismatched chart axes and false-success payloads",()=>{
  const chart={mode:"chart",title:"Alex",rationale:"query",traces:[{type:"bar",x:["Q1","Q2"],y:[5]}],layout:{}};
  expect(isGroundedExecution({...execution,chart})).toBe(false);
  expect(isGroundedExecution({...execution,recognized:false})).toBe(false);
 });
});
