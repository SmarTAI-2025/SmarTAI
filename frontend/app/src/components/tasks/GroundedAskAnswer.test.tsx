import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { GroundedAskAnswer } from "./GroundedAskAnswer";
import type { GroundedAskExecution } from "@/types";
it("shows an empty result without candidate selection or a clarification dialogue",()=>{
 const execution:GroundedAskExecution={recognized:true,kind:"table",explanation:"查询完成",data:{columns:["student_id"],rows:[]},selection:null,chart:null,candidates:[{kind:"student",text:"Alex",task_id:"task",student_id:"001",student_name:"Alex Chen"}]};
 render(<GroundedAskAnswer execution={execution} locale="zh-CN" />);
 expect(screen.getByText("没有匹配记录。")).toBeInTheDocument();
 expect(screen.queryByRole("button")).not.toBeInTheDocument();
});
