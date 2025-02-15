import json

from tqdm import tqdm
from vllm import LLM, SamplingParams
import torch
from transformers import AutoTokenizer
from datasets import load_dataset

from openrlhf.models import get_llm_for_sequence_regression
from openrlhf.datasets.answer_extraction import extract_last_single_answer, extract_math_answer

extract_answer = extract_math_answer

model_path = "/mnt/data/ckpt/pcl/qwen_full_lr5e-6_beta0-03_rew01_actor-loss-dro_kl-reg-unbiased1e-2_plot-weights"
tokenizer = AutoTokenizer.from_pretrained(model_path)
tokenizer.padding_side = "left"
tokenizer.pad_token = tokenizer.eos_token
critic = get_llm_for_sequence_regression(
    model_path + "_critic",
    "critic",
    normalize_reward=False,  # TODO: maybe experiment with this layer
    use_flash_attention_2=True,
    bf16=True,
    load_in_4bit=False,
    lora_rank=64,
    lora_alpha=64,
    lora_dropout=0,
    target_modules="all-linear",
    # ds_config=strategy.get_ds_train_config(is_actor=True),
)
critic.to("cuda")

# critic.load_adapter("/mnt/data/ckpt/dsm-inst_b2_lr1e-4_actor-lr5e-5_actor-loss-single-step_2epochs_critic", "default")

a = LLM(model_path, gpu_memory_utilization=0.4)
params = SamplingParams(temperature=1, stop=["\n", ". ", ".$ "], max_tokens=2048)

# d = load_dataset("openai/gsm8k", "main")
d = load_dataset("hendrycks/competition_math")
problem_idx = 20
question = d["test"][problem_idx]["problem"]
gt_answer = d["test"][problem_idx]["solution"]

messages = [
    {
        "role": "system",
        "content": "Please reason step by step, and put your final answer within \\boxed{}.",
    },
    {"role": "user", "content": question},
]

states = [tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)]
answers = []
K = 3
for _ in range(20):
    next_states = []
    for repeat_id in range(K):
        results = a.generate(states, params)
        completions = [result.outputs[0].text for result in results]
        input_token = tokenizer(
            [state + completion for state, completion in zip(states, completions)], padding=True, return_tensors="pt"
        )
        input_token.to("cuda")
        with torch.no_grad():
            values = critic(**input_token, action_mask=input_token["input_ids"])
        for i in range(len(completions)):
            tmp = states[i] + completions[i]
            if results[i].outputs[0].finish_reason == "stop" and results[i].outputs[0].stop_reason is not None:
                tmp += results[i].outputs[0].stop_reason
            next_states.append((tmp, values[i, -1].item(), results[i].outputs[0].stop_reason))
    next_states = sorted(next_states, key=lambda x: -x[1])[:K]
    states = []
    for state in next_states:
        if state[2] is None:
            answers.append(state)
        else:
            states.append(state[0])
    if len(states) == 0:
        break

__import__("pdb").set_trace()
answers = sorted(answers, key=lambda x: -x[1])
prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
pred = extract_answer(question, answers[0][0][len(prompt) :], "")
print(pred)

answer = extract_answer(question, gt_answer, "")
print(gt_answer)

from openrlhf.datasets.eval.eval_script import eval_math

print(eval_math({"prediction": pred, "answer": answer}))
