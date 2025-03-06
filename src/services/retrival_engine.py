import os
import pickle
import uuid
import hashlib
from typing import List, Dict, Any, Optional
import dotenv

from pinecone import Pinecone

from src.services.embedding_cache import EmbeddingCache
from src.config.pinecone_config import initialize_pinecone


class RetrivalEngine:
    """
    Recommendation engine using Pinecone vector database and cached embeddings.
    """

    def __init__(self, index_name: str = "recommendation-index", cache_dir: str = "./embedding_cache"):
        """
        Initialize the recommendation engine.
        
        Args:
            index_name (str): Name of the Pinecone index to use
            cache_dir (str): Directory to store the embedding cache
        """
        self.index_name = index_name
        self.cache_dir = cache_dir
        
        # Ensure cache directory exists
        os.makedirs(cache_dir, exist_ok=True)
        
        # Initialize embedding cache
        try:
            self.embedding_cache = EmbeddingCache(cache_dir=cache_dir)
        except Exception as e:
            print(f"Error initializing embedding cache: {e}")
            raise
        
        # Initialize Pinecone
        try:
            initialize_pinecone(index_name)
            pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
            self.index = pc.Index(index_name)
        except Exception as e:
            print(f"Error initializing Pinecone: {e}")
            raise
        
        # Initialize retrieval results cache
        self.retrieval_cache_file = os.path.join(cache_dir, "retrieval_cache.pkl")
        self.retrieval_cache = self._load_retrieval_cache()

    def _load_cache(self):
        cache_file = os.path.join(self.cache_dir, "embedding_cache.pkl")
        try:
            with open(cache_file, "rb") as f:
                self.embedding_cache = pickle.load(f)
        except (FileNotFoundError, EOFError):
            self.embedding_cache = {}

    def _save_cache(self):
        cache_file = os.path.join(self.cache_dir, "embedding_cache.pkl")
        try:
            with open(cache_file, "wb") as f:
                pickle.dump(self.embedding_cache, f)
        except Exception as e:
            print(f"Error saving cache: {e}")
            
    def _load_retrieval_cache(self) -> Dict[str, List[Dict[str, Any]]]:
        """Load the retrieval results cache from disk."""
        try:
            if os.path.exists(self.retrieval_cache_file):
                with open(self.retrieval_cache_file, "rb") as f:
                    cache = pickle.load(f)
                print(f"Loaded {len(cache)} cached retrieval results")
                return cache
            else:
                print("No existing retrieval cache found, starting fresh")
                return {}
        except Exception as e:
            print(f"Error loading retrieval cache: {e}")
            return {}
    
    def _save_retrieval_cache(self) -> None:
        """Save the retrieval results cache to disk."""
        try:
            with open(self.retrieval_cache_file, "wb") as f:
                pickle.dump(self.retrieval_cache, f)
            print(f"Saved {len(self.retrieval_cache)} retrieval results to cache")
        except Exception as e:
            print(f"Error saving retrieval cache: {e}")

    def _generate_retrieval_key(self, query: str, top_k: int, filter_criteria: Optional[Dict[str, Any]]) -> str:
        """Generate a unique key for a retrieval query."""
        # Convert filter_criteria to a stable string representation
        filter_str = str(sorted(filter_criteria.items())) if filter_criteria else "None"
        # Combine query parameters into a string and hash it
        key_str = f"{query}|{top_k}|{filter_str}"
        return hashlib.md5(key_str.encode('utf-8')).hexdigest()

    def add_item(self, content: str, metadata: Dict[str, Any], item_type: str) -> str:
        """
        Add a single item to the recommendation engine.
        
        Args:
            content (str): The text content to embed
            metadata (Dict[str, Any]): Metadata associated with the content
            item_type (str): Type of the item (e.g., 'article', 'product')
            
        Returns:
            str: ID of the added item
        """
        # Add content to metadata for retrieval
        metadata["content"] = content
        metadata["item_type"] = item_type
        
        # Generate embedding using the cache
        embedding = self.embedding_cache.get_embedding(content)
        
        # Generate a unique ID
        item_id = str(uuid.uuid4())
        
        # Upsert into Pinecone
        self.index.upsert(vectors=[
            {
                "id": item_id,
                "values": embedding,
                "metadata": metadata
            }
        ])
        
        # Clear retrieval cache since the index has been modified
        self.retrieval_cache = {}
        self._save_retrieval_cache()
        
        return item_id
    
    def bulk_add_items(
        self, 
        contents: List[str], 
        metadatas: List[Dict[str, Any]], 
        item_types: List[str]
    ) -> List[str]:
        """
        Add multiple items to the recommendation engine.
        
        Args:
            contents (List[str]): List of text contents to embed
            metadatas (List[Dict[str, Any]]): List of metadata dicts for each content
            item_types (List[str]): List of item types
            
        Returns:
            List[str]: List of IDs for the added items
        """
        if not (len(contents) == len(metadatas) == len(item_types)):
            raise ValueError("Contents, metadatas, and item_types must have the same length")

        try:
            # Get embeddings for all contents
            embeddings = []
            for content in contents:
                embedding = self.embedding_cache.get_embedding(content)
                embeddings.append(embedding)

            # Generate IDs and prepare vectors for Pinecone
            vectors = []
            for i, (content, metadata, embedding) in enumerate(zip(contents, metadatas, embeddings)):
                metadata["content"] = content
                metadata["item_type"] = item_types[i]
                vectors.append({
                    "id": f"item_{i}",
                    "values": embedding,
                    "metadata": metadata
                })

            # Upsert to Pinecone
            self.index.upsert(vectors=vectors)
            
            # Clear retrieval cache since the index has been modified
            self.retrieval_cache = {}
            self._save_retrieval_cache()
            
            return [v["id"] for v in vectors]
        
        except Exception as e:
            print(f"Error in bulk_add_items: {e}")
            raise
    
    def get_retrivals(
        self, 
        query: str, 
        top_k: int = 5,
        filter_criteria: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Get recommendations based on a query.
        
        Args:
            query (str): The query text
            top_k (int): Number of recommendations to return
            filter_criteria (Dict[str, Any], optional): Criteria to filter results
            
        Returns:
            List[Dict[str, Any]]: Recommended items with scores and metadata
        """
        # Generate a key for this query
        cache_key = self._generate_retrieval_key(query, top_k, filter_criteria)
        
        # Check if this query is already cached
        if cache_key in self.retrieval_cache:
            print(f"Using cached retrieval results for: {query[:30]}...")
            return self.retrieval_cache[cache_key]
        
        # Not in cache, proceed with normal retrieval
        print(f"Performing new retrieval for: {query[:30]}...")
        
        # Generate embedding for query using cache
        query_embedding = self.embedding_cache.get_embedding(query)
        
        # Query Pinecone
        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            include_metadata=True,
            filter=filter_criteria
        )
        
        # Format results
        recommendations = []
        for match in results["matches"]:
            recommendations.append({
                "id": match["id"],
                "score": match["score"],
                "metadata": match["metadata"]
            })
        
        # Cache the results
        self.retrieval_cache[cache_key] = recommendations
        self._save_retrieval_cache()
        
        return recommendations
    
    def delete_item(self, item_id: str) -> None:
        """
        Delete an item from the recommendation engine.
        
        Args:
            item_id (str): ID of the item to delete
        """
        self.index.delete(ids=[item_id])
        
        # Clear retrieval cache since the index has been modified
        self.retrieval_cache = {}
        self._save_retrieval_cache()
        
    def clear_retrieval_cache(self) -> None:
        """Clear the retrieval results cache."""
        self.retrieval_cache = {}
        self._save_retrieval_cache()
        print("Retrieval cache cleared.")
