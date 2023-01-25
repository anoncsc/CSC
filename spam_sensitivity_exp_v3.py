import argparse
import pickle
from random import sample

import torch
from torch import nn
from torch.optim import Adam
from torch.utils.data import TensorDataset, DataLoader

from data import load_spam_data
from delta import QuadraticCostDelta, create_cost_functions
from distribution import SequentialDistribution
from model import cross_entropy_loss_for_h, hinge_loss, MLP, TrickyFeatureModel
from records import get_chosen_model_and_multiple_rounds_stats
from sensitivity_exp_utils import generate_partitions_for_feature_drops
from training import NonStrategicTrainer, IterativeCausalStrategicTrainer, StrategicTrainer
from utils import set_seed, split_data, split_vector, create_Y, Data


def run(n_drops, seed, path):
    results_file = f"spam_sensitivity_exp_n_drops={n_drops}_seed={seed}"
    print(results_file, flush=True)
    total_dim = 15
    x1_features = [1, 8, 5]
    x2_features = [2, 3]
    u_features = x2_features + list(set([i for i in range(total_dim)]) - set(x1_features) - set(x2_features))
    features_order = x1_features + u_features
    x1_dim = len(x1_features)
    x2_dim = len(x2_features)
    u_dim = total_dim - x1_dim

    cserm_test_accs = []
    serm_test_accs = []
    max_partitions = 30
    possible_partitions = generate_partitions_for_feature_drops(x1_features, x2_features, u_features[:u_dim], n_drops)
    set_seed(seed)
    sampled_partitions = sample(possible_partitions, k=max_partitions) if len(possible_partitions) > max_partitions else possible_partitions
    for cur_x1_features, cur_x2_features, cur_u_features in sampled_partitions:
        if n_drops > 0:
            x1_features, x2_features, u_features = cur_x1_features, cur_x2_features, cur_u_features
            x1_dim, x2_dim, u_dim = len(x1_features), len(x2_features), len(u_features)
            features_order = x1_features + u_features

        def load_data(seed):
            X, Y = load_spam_data(seed, features_order)
            X = X[:, :x1_dim + u_dim]
            return X, Y

        set_seed(0)
        A = torch.rand(x2_dim, x2_dim)

        def u_to_x2(U):
            # keep only x2 features
            X2 = U[:, :x2_dim]
            # linear transform
            batch_matrix = A.expand(X2.shape[0], X2.shape[1], X2.shape[1])
            batch_vectors = torch.unsqueeze((X2), 2)
            X2 = torch.bmm(batch_matrix, batch_vectors)
            X2 = torch.squeeze(X2, 2)
            return X2

        # create h*
        # load data
        set_seed(0)
        X, Y = load_data(0)
        # split train-test
        X_train, Y_train, X_test, Y_test = split_data(X, Y, 0.2)
        X_train, Y_train, X_val, Y_val = split_data(X_train, Y_train, 0.4)

        # learning params
        batch_size = 64
        lr = 0.01
        epochs = 100
        tau = 4

        h_star_train_ds = TensorDataset(X_train, Y_train)
        h_star_train_dl = DataLoader(h_star_train_ds, batch_size=batch_size, shuffle=True)
        # h_star = MLP(in_dim=x1_dim + u_dim, out_dim=1, hidden_dim=10, hidden_layers=3)
        h_star = nn.Linear(x1_dim + u_dim, 1)
        opt = Adam(h_star.parameters(), lr=0.001)
        trainer = NonStrategicTrainer(h_star, h_star_train_dl, X_val, Y_val, X_test, Y_test, opt, cross_entropy_loss_for_h)
        trainer.train(100, early_stop=10)
        print("\nh_star creation:")
        trainer.test()

        m_h_star = TrickyFeatureModel(h_star, bad_feature=2, threshold=-0.05, gt=False, slope=20)

        # samples params
        time_steps = 10
        n_clean_train_samples = 1000
        n_samples_per_round = 200
        n_val_samples = 500
        n_test_samples = 2000

        n_features = x1_dim + x2_dim
        total_n_train_samples = n_clean_train_samples + time_steps * n_samples_per_round
        total_n_val_test_samples = n_val_samples + n_test_samples
        total_n_samples = total_n_train_samples + total_n_val_test_samples
        val_test_frac = total_n_val_test_samples / total_n_samples
        test_frac = n_test_samples / total_n_val_test_samples

        # create data
        X1_U, _ = load_data(0)
        X1, U = split_vector(X1_U, x1_dim)
        X2 = u_to_x2(U)
        X_U = torch.concat([X1, X2, U], 1)
        X, U = split_vector(X_U, n_features)
        Y = create_Y(X, U, m_h_star, x1_dim)
        # create balance set
        max_samples = min(len(Y[Y == 1]), len(Y[Y == -1]))

        X_U_pos = X_U[(Y == 1).view(-1), :]
        X_U_pos = X_U_pos[:max_samples]
        Y_pos = Y[(Y == 1).view(-1)]
        Y_pos = Y_pos[:max_samples]

        X_U_neg = X_U[(Y == -1).view(-1), :]
        X_U_neg = X_U_neg[:max_samples]
        Y_neg = Y[(Y == -1).view(-1)]
        Y_neg = Y_neg[:max_samples]

        X_U, Y = torch.concat([X_U_pos, X_U_neg], 0), torch.concat([Y_pos, Y_neg], 0)
        # shuffle rows
        perm = torch.randperm(X_U.size()[0])
        X_U, Y = X_U[perm], Y[perm]

        # split data to train, val, test
        X_U_train, Y_train, X_U_val, Y_val = split_data(X_U, Y, val_test_frac)
        X_U_val, Y_val, X_U_test, Y_test = split_data(X_U_val, Y_val, test_frac)
        X_train, U_train = split_vector(X_U_train, n_features)
        X_val, U_val = split_vector(X_U_val, n_features)
        X_test, U_test = split_vector(X_U_test, n_features)

        full_train_dl = DataLoader(TensorDataset(X_train, Y_train), batch_size=batch_size, shuffle=True)

        # create datasets objects
        clean_data = Data(X_train[:n_clean_train_samples], Y_train[:n_clean_train_samples], U_train[:n_clean_train_samples], X_val, Y_val, U_val, X_test, Y_test, U_test)
        full_data = Data(X_train, Y_train, U_train, X_val, Y_val, U_val, X_test, Y_test, U_test)

        def f_fac():
            set_seed(seed)
            return nn.Linear(n_features, 1)

        def h_fac():
            set_seed(seed)
            return MLP(in_dim=n_features, out_dim=1, hidden_dim=10, hidden_layers=3)

        cost_scale = 40
        cost, _, _ = create_cost_functions(cost_scale, x1_dim, x2_dim)

        set_seed(seed)
        delta = QuadraticCostDelta(None, cost, tau)
        # create dist object from the samples that not included in the partial clean data
        s_dist = SequentialDistribution(x1_dim, X_U_train[n_clean_train_samples:])
        cserm_trainer = IterativeCausalStrategicTrainer(f_fac, h_fac, m_h_star, delta, s_dist, clean_data, x1_dim, x2_dim, n_samples_per_round, hinge_loss,
                                                        cross_entropy_loss_for_h,
                                                        exploration_params=None, should_estimate_density=True, pca=None, cost_scale=cost_scale)
        cserm_trainer.train(time_steps, epochs, lr, lr, batch_size, verbose=True)
        cserm_chosen_model_stats, cserm_multi_rounds_stats = get_chosen_model_and_multiple_rounds_stats(cserm_trainer.results, "CSERM")
        cserm_test_accs.append(cserm_chosen_model_stats["accuracy_test"].iloc[0])

        # serm
        f = f_fac()
        opt = Adam(f.parameters(), lr=lr)
        delta = QuadraticCostDelta(f, cost, tau)
        serm_trainer = StrategicTrainer(f, delta, full_train_dl, X_val, Y_val, X_test, Y_test, opt, hinge_loss, cost_scale=cost_scale)
        serm_trainer.train(epochs, True)
        serm_chosen_model_stats = serm_trainer.collect_trainer_results(m_h_star, delta, full_data, x1_dim).calc_stats("SERM")
        serm_test_accs.append(serm_chosen_model_stats["accuracy_test"].iloc[0])

    # save results
    results = {"cserm_test_accs": cserm_test_accs, "serm_test_accs": serm_test_accs}
    with open(f"{path}/{results_file}.pkl", "wb") as file:
        pickle.dump(results, file)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('n_drops')
    parser.add_argument('seed')
    parser.add_argument('path')
    args = parser.parse_args()
    n_drops = int(args.n_drops)
    seed = int(args.seed)
    path = args.path
    run(n_drops, seed, path)
