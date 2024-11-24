import torch
import pandas as pd
import numpy as np
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder, StandardScaler
from collections import Counter
import torch.nn as nn
from sklearn.metrics import precision_score, recall_score

def calculate_rmse(pred, true):
    # Squeeze pred to match true's dimensions
    pred = pred.squeeze()
    return torch.sqrt(nn.MSELoss()(pred, true)).item()

def calculate_mae(pred, true):
    # Squeeze pred to match true's dimensions
    pred = pred.squeeze()
    return nn.L1Loss()(pred, true).item()

def calculate_precision_recall(interaction_pred, label_true, threshold=0.5):
    pred_labels = (interaction_pred.squeeze() >= threshold).cpu().numpy()
    true_labels = label_true.cpu().numpy()
    precision = precision_score(true_labels, pred_labels, zero_division=0)
    recall = recall_score(true_labels, pred_labels, zero_division=0)
    return precision, recall

class BalancedMovieLensDataset(Dataset):
    def __init__(self, users, movies, ratings, transform=None):
        self.users = users
        self.movies = movies
        self.ratings = ratings
        self.transform = transform
        self.normalized_ratings = (ratings - 1) / 4.0
        self.labels = (ratings >= 4.0).astype(np.int32)

    def __len__(self):
        return len(self.users)

    def __getitem__(self, idx):
        if self.transform:
            rating = self.transform(self.normalized_ratings[idx])
        else:
            rating = self.normalized_ratings[idx]
        return {
            "users": torch.tensor(self.users[idx], dtype=torch.long),
            "movies": torch.tensor(self.movies[idx], dtype=torch.long),
            "ratings": torch.tensor(rating, dtype=torch.float),
            "labels": torch.tensor(self.labels[idx], dtype=torch.float)
        }

class ResidualBlock(nn.Module):
    def __init__(self, layer):
        super().__init__()
        self.layer = layer
    
    def forward(self, x):
        return x + self.layer(x)

class DeepRecommenderModel(nn.Module):
    def __init__(self, num_users, num_movies, embedding_dim=128, layers=[256, 128, 64], dropout_rate=0.2):
        super(DeepRecommenderModel, self).__init__()
        
        self.user_embedding = nn.Embedding(num_users, embedding_dim)
        self.movie_embedding = nn.Embedding(num_movies, embedding_dim)
        
        self.user_bias = nn.Embedding(num_users, 1)
        self.movie_bias = nn.Embedding(num_movies, 1)
        
        self.mlp_layers = []
        input_dim = embedding_dim * 3  # Changed for element-wise multiplication

        self.residual_layers = nn.ModuleList()
        for layer_dim in layers:
            layer = nn.Sequential(
                nn.Linear(input_dim, layer_dim),
                nn.LayerNorm(layer_dim),
                nn.ReLU(),
                nn.Dropout(dropout_rate)
            )
            self.residual_layers.append(layer)
            if input_dim == layer_dim:
                self.mlp_layers.append(ResidualBlock(layer))
            else:
                self.mlp_layers.append(layer)
            input_dim = layer_dim

        self.mlp = nn.Sequential(*self.mlp_layers)

        self.rating_predictor = nn.Sequential(
            nn.Linear(layers[-1], layers[-1]//2),
            nn.ReLU(),
            nn.Dropout(dropout_rate),
            nn.Linear(layers[-1]//2, 1),
            nn.Sigmoid()
        )

        self.interaction_predictor = nn.Sequential(
            nn.Linear(layers[-1], layers[-1]//2),
            nn.ReLU(),
            nn.Dropout(dropout_rate),
            nn.Linear(layers[-1]//2, 1),
            nn.Sigmoid()
        )

        self.apply(self._init_weights)

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            nn.init.xavier_normal_(module.weight, gain=1.0)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            nn.init.normal_(module.weight, mean=0.0, std=0.01)

    def forward(self, users, movies):
        user_emb = self.user_embedding(users)
        movie_emb = self.movie_embedding(movies)
        
        user_bias = self.user_bias(users)
        movie_bias = self.movie_bias(movies)
        
        element_wise = user_emb * movie_emb
        concat = torch.cat([user_emb, movie_emb, element_wise], dim=1)
        
        features = self.mlp(concat)
        
        rating_pred = self.rating_predictor(features) * 4.0 + 1.0 + user_bias + movie_bias
        interaction_pred = self.interaction_predictor(features)
        
        return rating_pred, interaction_pred

def create_balanced_sampler(dataset):
    labels = dataset.labels
    class_counts = Counter(labels)
    total_samples = len(labels)

    class_weights = {class_id: total_samples / count for class_id, count in class_counts.items()}
    sample_weights = [class_weights[label] for label in labels]

    sampler = WeightedRandomSampler(
        weights=sample_weights,
        num_samples=len(sample_weights),
        replacement=True
    )
    return sampler

def custom_loss(rating_pred, interaction_pred, rating_true, label_true, alpha=0.7):
    mse_loss = nn.MSELoss()(rating_pred.squeeze(), rating_true)
    bce_loss = nn.BCELoss()(interaction_pred.squeeze(), label_true)
    mae_loss = nn.L1Loss()(rating_pred.squeeze(), rating_true)
    return alpha * (0.8 * mse_loss + 0.2 * mae_loss) + (1 - alpha) * bce_loss

def calculate_precision(model, user_ids, movie_ids, true_interactions, device, top_k=10):
    model.eval()
    precision_scores = []

    with torch.no_grad():
        for user_id in user_ids:
            user_movies = movie_ids[true_interactions[user_id] > 0]
            all_movies = torch.arange(len(movie_ids)).to(device)
            user_tensor = torch.full((len(all_movies),), user_id).to(device)

            rating_pred, _ = model(user_tensor, all_movies)
            top_k_movies = torch.topk(rating_pred.squeeze(), top_k).indices.cpu().numpy()

            watched_movies = set(torch.tensor(user_movies).cpu().numpy())
            recommended_movies = set(top_k_movies)

            precision = len(watched_movies & recommended_movies) / top_k
            precision_scores.append(precision)

    return np.mean(precision_scores)

def evaluate_model(model, dataloader, device, movie_ids):
    model.eval()
    val_loss = rmse = mae = precision = recall = 0
    true_interactions = {user_id: torch.zeros(len(movie_ids)) for user_id in dataloader.dataset.users}

    with torch.no_grad():
        for batch in dataloader:
            users = batch["users"].to(device)
            movies = batch["movies"].to(device)
            ratings = batch["ratings"].to(device)
            labels = batch["labels"].to(device)

            for user, movie, label in zip(users, movies, labels):
                true_interactions[user.item()][movie.item()] = label

            rating_pred, interaction_pred = model(users, movies)
            loss = custom_loss(rating_pred, interaction_pred, ratings, labels)
            val_loss += loss.item()

            rmse += calculate_rmse(rating_pred, ratings)
            mae += calculate_mae(rating_pred, ratings)

    precision = calculate_precision(model, dataloader.dataset.users, movie_ids, true_interactions, device)

    n = len(dataloader)
    return (val_loss/n, rmse/n, mae/n, precision, recall/n)

def train_model(model, train_loader, val_loader, movie_ids, epochs=15, device="cuda"):
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.001, weight_decay=0.01)
    
    scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
        optimizer, T_0=5, T_mult=2, eta_min=1e-6
    )

    best_val_loss = float('inf')
    patience = 5
    patience_counter = 0

    for epoch in range(epochs):
        model.train()
        total_loss = 0
        
        for batch in train_loader:
            users = batch["users"].to(device)
            movies = batch["movies"].to(device)
            ratings = batch["ratings"].to(device)
            labels = batch["labels"].to(device)

            optimizer.zero_grad()
            rating_pred, interaction_pred = model(users, movies)
            loss = custom_loss(rating_pred, interaction_pred, ratings, labels)
            loss.backward()

            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            total_loss += loss.item()

        scheduler.step()
        
        avg_train_loss = total_loss / len(train_loader)
        
        model.eval()
        val_loss, rmse, mae, precision, recall = evaluate_model(model, val_loader, device, movie_ids)
        
        print(f"\nEpoch {epoch+1}/{epochs}")
        print(f"Average Training Loss: {avg_train_loss:.4f}")
        print(f"Validation RMSE: {rmse:.4f}, MAE: {mae:.4f}")
        print(f"Validation Precision: {precision:.4f}, Recall: {recall:.4f}")

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            torch.save(model.state_dict(), 'best_model.pth')
            patience_counter = 0
        else:
            patience_counter += 1
            if patience_counter >= patience:
                print("Early stopping triggered")
                break

def prepare_data(df):
    # Filter users with at least 5 ratings
    user_counts = df['userId'].value_counts()
    valid_users = user_counts[user_counts >= 5].index
    df = df[df['userId'].isin(valid_users)]

    # Filter movies with at least 3 ratings
    movie_counts = df['movieId'].value_counts()
    valid_movies = movie_counts[movie_counts >= 3].index
    df = df[df['movieId'].isin(valid_movies)]

    # Add user and movie average ratings
    user_avg = df.groupby('userId')['rating'].mean()
    movie_avg = df.groupby('movieId')['rating'].mean()
    df['user_avg_rating'] = df['userId'].map(user_avg)
    df['movie_avg_rating'] = df['movieId'].map(movie_avg)

    # Balance the dataset
    positive_samples = df[df['rating'] >= 4.0]
    negative_samples = df[df['rating'] < 4.0]

    if len(negative_samples) > 1.5 * len(positive_samples):
        negative_samples = negative_samples.sample(
            n=int(1.5 * len(positive_samples)),
            random_state=42
        )

    balanced_df = pd.concat([positive_samples, negative_samples])
    return balanced_df

def get_predictions_for_user(user_id, model_path, df, le_user, le_movie):
    """
    Get predictions for a specific user using a trained model
    Args:
        user_id: User ID to get predictions for
        model_path: Path to saved model weights
        df: DataFrame containing movie data
        le_user: Fitted LabelEncoder for users
        le_movie: Fitted LabelEncoder for movies
    Returns:
        DataFrame with movie predictions
    """
    # Initialize model with same architecture as training
    model = DeepRecommenderModel(
        num_users=len(le_user.classes_),
        num_movies=len(le_movie.classes_),
        embedding_dim=128
    )
    
    # Load saved model weights
    try:
        model.load_state_dict(torch.load(model_path, weights_only=True))
    except FileNotFoundError:
        raise FileNotFoundError(f"Model file not found at {model_path}")
        
    # Move model to same device as training
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model.to(device)
    model.eval()
    
    # Get unique movies
    movies = df['movieId'].unique()
    
    # Transform IDs using label encoders
    encoded_user_id = le_user.transform([user_id])[0]
    encoded_movies = le_movie.transform(movies)
    
    # Create tensors for prediction
    user_ids = torch.full((len(movies),), encoded_user_id)
    movie_ids = torch.tensor(encoded_movies)
    
    # Get predictions
    with torch.no_grad():
        rating_pred, _ = model(user_ids.to(device), movie_ids.to(device))
    
    # Create results dataframe
    results_df = pd.DataFrame({
        'movieId': movies,
        'predicted_rating': rating_pred.cpu().numpy().squeeze()
    })
    
    return results_df.sort_values('predicted_rating', ascending=False)

def main():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    # Load and prepare data
    df = pd.read_csv("databases/ml-latest-small/ratings.csv")
    df = prepare_data(df)

    # Split data
    df_train, df_val = train_test_split(df, test_size=0.1, random_state=42)

    # Encode users and movies
    le_user = LabelEncoder()
    le_movie = LabelEncoder()

    le_user.fit(pd.concat([df_train['userId'], df_val['userId']]))
    le_movie.fit(pd.concat([df_train['movieId'], df_val['movieId']]))

    df_train['userId'] = le_user.transform(df_train['userId'])
    df_train['movieId'] = le_movie.transform(df_train['movieId'])
    df_val['userId'] = le_user.transform(df_val['userId'])
    df_val['movieId'] = le_movie.transform(df_val['movieId'])

    # Get unique movie IDs
    movie_ids = df['movieId'].unique()

    # Create datasets
    train_dataset = BalancedMovieLensDataset(
        users=df_train['userId'].values,
        movies=df_train['movieId'].values,
        ratings=df_train['rating'].values
    )

    val_dataset = BalancedMovieLensDataset(
        users=df_val['userId'].values,
        movies=df_val['movieId'].values,
        ratings=df_val['rating'].values
    )

    # Create data loaders
    train_sampler = create_balanced_sampler(train_dataset)

    train_loader = DataLoader(
        train_dataset, 
        batch_size=64, 
        sampler=train_sampler,
        num_workers=4
    )
    val_loader = DataLoader(
        val_dataset, 
        batch_size=64, 
        shuffle=False,
        num_workers=4
    )
    
    # Initialize model
    model = DeepRecommenderModel(
        num_users=len(le_user.classes_),
        num_movies=len(le_movie.classes_),
        embedding_dim=128,
        layers=[256, 128, 64]
    ).to(device)
    
    # Train model
    train_model(model, train_loader, val_loader, movie_ids, epochs=15, device=device)

    # After training is complete:
    user_id_20 = le_user.transform([20])[0]  # Get encoded value for user 20
    predictions = get_predictions_for_user(user_id_20, 'best_model.pth', df, le_user, le_movie)
    print(predictions.head())
if __name__ == "__main__":
    main()