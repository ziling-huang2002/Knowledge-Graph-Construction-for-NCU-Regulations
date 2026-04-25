"""Minimal KG query template for Assignment 4.

Keep these APIs unchanged for auto-test:
- generate_text(messages, max_new_tokens=220)
- get_relevant_articles(question)
- generate_answer(question, rule_results)

Keep Rule fields aligned with build_kg output:
rule_id, type, action, result, art_ref, reg_name
"""

import os
import re
from typing import Any

from neo4j import GraphDatabase
from dotenv import load_dotenv

from llm_loader import load_local_llm, get_tokenizer, get_raw_pipeline


# ========== 0) Initialization ==========
load_dotenv()

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
AUTH = (
	os.getenv("NEO4J_USER", "neo4j"),
	os.getenv("NEO4J_PASSWORD", "password"),
)

# Avoid local proxy settings interfering with model/Neo4j access.
for key in ["http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
	if key in os.environ:
		del os.environ[key]


try:
	driver = GraphDatabase.driver(URI, auth=AUTH)
	driver.verify_connectivity()
except Exception as e:
	print(f"⚠️ Neo4j connection warning: {e}")
	driver = None


# ========== 1) Public API (query flow order) ==========
# Order: extract_entities -> build_typed_cypher -> get_relevant_articles -> generate_answer

def generate_text(messages: list[dict[str, str]], max_new_tokens: int = 220) -> str:
	"""
	Call local HF model via chat template + raw pipeline.

	Interface:
	- Input:
	  - messages: list[dict[str, str]] (chat messages with role/content)
	  - max_new_tokens: int
	- Output:
	  - str (model generated text, no JSON guarantee)
	"""
	tok = get_tokenizer()
	pipe = get_raw_pipeline()
	if tok is None or pipe is None:
		load_local_llm()
		tok = get_tokenizer()
		pipe = get_raw_pipeline()
	prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
	return pipe(prompt, max_new_tokens=max_new_tokens)[0]["generated_text"].strip()


def extract_entities(question: str) -> dict[str, Any]:
	"""TODO(student, required): parse question to {question_type, subject_terms, aspect}."""
	"""利用 LLM 提取問題中的關鍵字與意圖。"""
	prompt = f"""Extract search keywords and the core aspect from this question about NCU regulations.
	Question: {question}
	Return JSON format: {{"question_type": "...", "subject_terms": ["keyword1", "keyword2"], "aspect": "..."}}
	"""
	messages = [{"role": "user", "content": prompt}]
	try:
		# 這裡調用 generate_text
		response = generate_text(messages, max_new_tokens=100)
		# 簡單解析 JSON (可加入類似 build_kg 的清洗邏輯)
		import json
		return json.loads(response)
	except:
		return {"question_type": "general", "subject_terms": [question], "aspect": "general"}


def build_typed_cypher(entities: dict[str, Any]) -> tuple[str, str]:
	"""TODO(student, required): return (typed_query, broad_query) with score and required fields."""
	# 這裡我們不只搜 rule_idx，同時搜尋 article_content_idx
	keywords = " OR ".join(entities.get("subject_terms", []))
	if not keywords: keywords = "NCU"

	# 修改後的 Cypher：同時從 Article 內容找，也從 Rule 找
	cypher_typed = """
	CALL db.index.fulltext.queryNodes("article_content_idx", $term) YIELD node AS a, score
	OPTIONAL MATCH (a)-[:CONTAINS_RULE]->(r:Rule)
	RETURN a.content AS content, 
			CASE WHEN r IS NOT NULL THEN r.action ELSE "General" END AS action,
			CASE WHEN r IS NOT NULL THEN r.result ELSE "General" END AS result, 
			score
	ORDER BY score DESC LIMIT 5
	"""
	return cypher_typed, keywords


def clean_query(text: str) -> str:
    """過濾掉會導致 Neo4j 全文本檢索報錯的特殊字元"""
    # 將特殊符號替換成空白，避免 Lucene 解析錯誤
    special_chars = r'[\+\-\&\|\!\(\)\{\}\[\]\^\"\~\*\?\:\\\/]'
    return re.sub(special_chars, ' ', text)


def get_relevant_articles(question: str) -> list[dict[str, Any]]:
	"""TODO(student, required): run typed+broad retrieval and return merged rule dicts."""

	if driver is None: return []
	
	entities = extract_entities(question)
	typed_query, raw_keywords = build_typed_cypher(entities)

	# 關鍵：在這裡執行清洗
	cleaned_term = clean_query(raw_keywords)

	results = []
	seen_content = set()
	with driver.session() as session:
		# 使用參數化查詢 term=$term
		records = session.run(typed_query, term=cleaned_term) 
		for rec in records:
			if rec["content"] not in seen_content:
				seen_content.add(rec["content"])
				results.append({
					"content": rec["content"],
					"rule": f"Action: {rec['action']}, Result: {rec['result']}"
				})
	return results[:3]


def generate_answer(question: str, rule_results: list[dict[str, Any]]) -> str:
	"""將檢索到的法規作為背景知識，交給 LLM 生成答案。"""
	if not rule_results:
		return "Insufficient rule evidence to answer this question."

	# 組合背景知識
	context = "\n".join([f"Article Content: {res['content']}\nExtracted Rule: {res['rule']}" for res in rule_results])

	messages = [
		{
			"role": "system", 
			"content": (
				"You are an official NCU Regulation Assistant. "
				"1. Answer based ONLY on the provided context. "
				"2. If the answer is not in the context, say you don't know. "
				"3. Use a professional and direct tone."
			)
		},
		{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}
	]
	return generate_text(messages)


def main() -> None:
	"""Interactive CLI (provided scaffold)."""
	if driver is None:
		return

	load_local_llm()

	print("=" * 50)
	print("🎓 NCU Regulation Assistant (Template)")
	print("=" * 50)
	print("💡 Try: 'What is the penalty for forgetting student ID?'")
	print("👉 Type 'exit' to quit.\n")

	while True:
		try:
			user_q = input("\nUser: ").strip()
			if not user_q:
				continue
			if user_q.lower() in {"exit", "quit"}:
				print("👋 Bye!")
				break

			results = get_relevant_articles(user_q)
			answer = generate_answer(user_q, results)
			print(f"Bot: {answer}")

		except KeyboardInterrupt:
			print("\n👋 Bye!")
			break
		except NotImplementedError as e:
			print(f"⚠️ {e}")
			break
		except Exception as e:
			print(f"❌ Error: {e}")

	driver.close()


if __name__ == "__main__":
	main()

