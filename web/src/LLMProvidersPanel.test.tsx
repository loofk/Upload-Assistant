import {afterEach, expect, it, vi} from "vitest";
import {cleanup, render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {ApiClient} from "./api";
import LLMProvidersPanel from "./LLMProvidersPanel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("configures AI providers with a 600 second local timeout ceiling", async () => {
  const provider = {id:"22222222-2222-4222-8222-222222222222",name:"sub2api",kind:"openai_compatible",base_url:"https://models.example.invalid/v1",model:"reasoner",data_level:"remote",api_mode:"chat_completions",reasoning_effort:"high",use_cases:["incident_diagnosis","rule_analysis"],json_mode:true,timeout_seconds:300,enabled:true,outbound_consent:true,api_key_configured:true,health_status:"ready",capabilities:{catalog_source:"provider_models",models:[]},last_probe_evidence:{}};
  vi.stubGlobal("fetch",vi.fn().mockResolvedValue(new Response(JSON.stringify({llm_providers:[provider]}),{status:200,headers:{"Content-Type":"application/json"}})));
  render(<LLMProvidersPanel client={new ApiClient("ua_test")} onError={(reason)=>{throw reason;}}/>);
  await userEvent.click(await screen.findByRole("button",{name:/sub2api/}));
  expect(screen.getByRole("heading",{name:"编辑模型 · sub2api"})).toBeInTheDocument();
  expect(screen.getByLabelText("模型")).toHaveValue("reasoner");
  expect(screen.getByRole("checkbox", {name: /自定义调用参数/})).toBeChecked();
  expect(screen.getByLabelText("超时")).toHaveValue("300");
  expect(screen.getByRole("checkbox", {name: "使用流式响应"})).toBeChecked();
  expect(screen.getByText("默认使用流式 JSON 输出和 600 秒总预算。")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", {name: "关闭"}));
  await userEvent.click(screen.getByRole("button", {name: "新增模型"}));
  expect(screen.getByRole("heading", {name: "新增 AI 模型"})).toBeInTheDocument();
  expect(screen.getByRole("checkbox", {name: /自定义调用参数/})).not.toBeChecked();
  expect(screen.queryByLabelText("超时")).not.toBeInTheDocument();
});
