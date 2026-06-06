import os
import glob
import pandas as pd
import jieba
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import re

CONCLUSION_DIR = "../../conclusion"
SRC_DIR = "../../src"
CREWLIST_PATH = "../../_local/notes/crewlist.md"

class SearchEngine:
    def __init__(self):
        self.documents = []
        self.vectorizer = TfidfVectorizer(tokenizer=self.jieba_tokenizer, token_pattern=None)
        self.tfidf_matrix = None
        self.load_data()

    def jieba_tokenizer(self, text):
        # Silent but could be expensive
        return list(jieba.cut(text))

    def reload_data(self):
        self.documents = []
        self.load_data()

    def load_data(self):
        print("SearchEngine: Loading data...")
        self.documents = []
        
        # 1. Load Conclusion Index (Primary)
        index_path = os.path.join(CONCLUSION_DIR, "index.md")
        if os.path.exists(index_path):
            print("SearchEngine: Loading index.md...")
            with open(index_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            # Pre-parse topic files to cache content
            # Mapping topic_file -> {key: content}
            topic_cache = {}
            
            start_parsing = False
            for line in lines:
                if "| --- |" in line:
                    start_parsing = True
                    continue
                if not start_parsing:
                    continue
                    
                if "|" not in line: continue
                
                parts = [p.strip() for p in line.strip().split("|")]
                if len(parts) >= 5:
                    key = parts[1]
                    summary = parts[2]
                    topic_link = parts[3]
                    
                    topic_file = ""
                    if "(" in topic_link and ")" in topic_link:
                        topic_file = topic_link.split("(")[1].split(")")[0]
                    
                    # Extract content from topic file if not cached
                    content = ""
                    if topic_file:
                        if topic_file not in topic_cache:
                            topic_cache[topic_file] = self._parse_topic_file(topic_file)
                        content = topic_cache[topic_file].get(key, "Content not found.")

                    self.documents.append({
                        "key": key,
                        "display_summary": summary,
                        "search_text": f"{key} {summary}",
                        "topic_file": topic_file,
                        "content": content,
                        "full_text": content,
                        "source": "conclusion",
                        "score": 0.0
                    })

        # 2. Load Raw Source (Fallback)
        self.load_source_data()

        print(f"SearchEngine: Loaded {len(self.documents)} documents. Starting TF-IDF indexing...")
        if self.documents:
            corpus = [doc["search_text"] for doc in self.documents]
            self.tfidf_matrix = self.vectorizer.fit_transform(corpus)
            print("SearchEngine: Indexing complete.")

    def _parse_topic_file(self, topic_file):
        """Parse a topic file and return a dict of {key: content}"""
        res = {}
        filepath = os.path.join(CONCLUSION_DIR, topic_file)
        if not os.path.exists(filepath):
            return res
            
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # Split by ## [Key]
        sections = re.split(r'\n(## \[.*?\])', "\n" + content)
        # sections[0] is header, then [header, content, header, content...]
        for i in range(1, len(sections), 2):
            header = sections[i]
            body = sections[i+1] if i+1 < len(sections) else ""
            
            # Extract key from ## [Key]
            match = re.search(r'\[(.*?)\]', header)
            if match:
                key = match.group(1)
                res[key] = (header + body).strip()
        return res

    def load_source_data(self):
        if not os.path.exists(SRC_DIR):
            return
        
        for filename in os.listdir(SRC_DIR):
            if filename.endswith(".csv"):
                try:
                    df = pd.read_csv(os.path.join(SRC_DIR, filename))
                    for _, row in df.iterrows():
                        if '问题关键字' in row and pd.notna(row['问题关键字']):
                            key = str(row['问题关键字'])
                            summary = str(row['概要']) if '概要' in row else ""
                            # Check if already in conclusion
                            if any(d['key'] == key for d in self.documents):
                                continue
                                
                            item_content = str(dict(row))
                            self.documents.append({
                                "key": key,
                                "display_summary": summary,
                                "search_text": f"{key} {summary}",
                                "content": item_content,
                                "full_text": item_content,
                                "source": "src",
                                "score": 0.0
                            })
                except:
                    pass

    def search(self, query: str):
        if not self.tfidf_matrix is not None and not self.documents:
            return {"type": "none", "results": []}

        # Transform query
        query_vec = self.vectorizer.transform([query])
        
        # Calculate cosine similarity
        cosine_sim = cosine_similarity(query_vec, self.tfidf_matrix).flatten()
        
        # Get top results
        results = []
        related_docs_indices = cosine_sim.argsort()[::-1]
        
        for idx in related_docs_indices:
            score = cosine_sim[idx]
            if score < 0.1: # Cutoff
                break
                
            doc = self.documents[idx]
            doc_copy = doc.copy()
            doc_copy['score'] = float(score)
            results.append(doc_copy)
            
            if len(results) >= 10:
                break
                
        # Determine strict type
        if results and results[0]['score'] > 0.7 and results[0]['source'] == 'conclusion':
            return {"type": "conclusion", "results": results}
        elif results:
            return {"type": "mixed", "results": results}
        else:
            return {"type": "none", "results": []}

    # fill_content is no longer needed but kept for compatibility if called elsewhere
    def fill_content(self, doc_copy):
        pass
