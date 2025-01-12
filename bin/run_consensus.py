#!/usr/bin/env python

"""
Author: Raquel Manzano - @RaqManzano
Script: Performs a consensus with results from different variant callers
"""
import argparse
import pandas as pd
import numpy as np
import gzip


def argparser():
    parser = argparse.ArgumentParser(description='')
    parser.add_argument("--input", "-i", help="Input VCF files, space separated", nargs='+')
    parser.add_argument("--names", "-n", help="Caller names (if not given numbers will be assigned), space separated",
                        nargs='+')
    parser.add_argument("--indel_window", help="Window to look for indels overlap", default=3)
    parser.add_argument("--prefix", help="prefix for output files, e.g. {prefix}.vcf", default='consensus')
    parser.add_argument("--maf", help="If input is maf select this parameter", action='store_true')
    return parser.parse_args()


def open_vcf(vcf):
    if vcf.endswith("gz"):
        vcf = gzip.open(vcf, mode="rt")
    else:
        vcf = open(vcf, mode="r")
    return vcf


def maf_to_pandas(maf_file):
    maf = pd.read_csv(maf_file, sep="\t", comment="#")
    return maf


def vcf_to_pandas(vcf_file):
    meta = ''
    variants = []
    vcf = open_vcf(vcf_file)
    for line in vcf:
        if line.startswith("##"):
            meta += line
        elif line.startswith("#"):
            header = line.strip().split("\t")
        else:
            variants += [line.strip().split("\t")]
    vcf.close()
    # print(variants)
    try:
        df = pd.DataFrame(variants, columns=header)
    except ValueError:
        df = pd.read_csv(vcf_file, sep="\t", comment="#")
    except UnboundLocalError:
        header = ''
        df = pd.read_csv(vcf_file, sep="\t", comment="#")

    return (meta, header, df)


def add_varid_and_type(df, caller, chrom, pos, ref, alt):
    """Adds a variant id using chrom:g.posREF>ALT format"""
    try:
        df['DNAchange'] = df[chrom].map(str) + ":g." + df[pos].map(str) + df[ref] + ">" + df[alt]
    except KeyError:
        pos = "Start_position"  # common error in maf format
        df["Start_Position"] = df["Start_position"]  # to fix downstream if this typo happens
        df['DNAchange'] = df[chrom].map(str) + ":g." + df[pos].map(str) + df[ref] + ">" + df[alt]
    df['Type'] = np.where((df[ref].str.len() == 1) & (df[alt].str.len() == 1), 'SNV',
                          np.where((df[ref].str.len() == df[alt].str.len()), "MNP",
                                   np.where((df[ref].str.len() > df[alt].str.len()), "DEL", "INS")
                                   )
                          )
    df["Caller"] = caller
    calls = {"SNVs": df[df["Type"] == "SNV"],
             "MNPs": df[df["Type"] == "MNP"],
             "INDELs": df[df["Type"].str.contains("INS|DEL")]}
    return calls


def assign_callers_names(caller_names, vcfs):
    """
    Assign a name to a caller
    :param caller_names:
    :param vcfs:
    :return:
    """
    if not caller_names:
        caller_names = ["CALLER" + str(idx + 1) for idx, _ in enumerate(vcfs)]
    print("- Callers names: " + ' - '.join(caller_names))
    return caller_names


def consensus_exact_match(list_of_calls, threshold=2):
    """
    Consensus for SNVs-like variants, exact match will be expected.
    :param list_of_calls: a list of pandas dataframes from a VCF
    :param threshold:  minimum number of callers to assume a consensus
    :return: a list of pandas dataframes
    """
    # extract variant id to match between call set
    to_concat = []
    for df in list_of_calls:
        to_concat += [df['DNAchange']]
    # count how many teams we see this variant
    all_snvs = pd.concat(to_concat)
    # count how many teams we see this variant
    all_snvs = all_snvs.value_counts().rename_axis('DNAchange').reset_index(name='consensus_count')
    # if count >= threshold then we will assume is a consensus
    in_consensus = all_snvs['consensus_count'] >= threshold
    consensus_snvs = all_snvs['DNAchange'][in_consensus]
    # feed that info back to dataframe
    list_of_calls_with_consensus = []
    for calls in list_of_calls:
        calls = calls.assign(consensus=calls['DNAchange'].isin(consensus_snvs))
        calls = calls.assign(overlap=np.where(calls['consensus'] == True, "ABSOLUTE", "NONE"))
        calls = calls.assign(overlap_id='.')
        calls = pd.merge(calls, all_snvs, on='DNAchange')
        list_of_calls_with_consensus += [calls]

    return list_of_calls_with_consensus


def consensus_overlap(list_of_calls, caller_names, window=3, chrom='#CHROM', pos="POS", alt="ALT"):
    """
    Assigns a consensus type when variants overlap in a 3bp window
    :param list_of_calls: list of pandas dataframes
    :param window: integer
    :return: list of pandas dataframes
    """
    indels = []
    for df in list_of_calls:
        indels += [df]
    # make sure it is sorted for iteration
    all_indels = pd.concat(indels)
    all_indels[pos] = all_indels[pos].astype(int)
    all_indels_sorted = all_indels.sort_values([chrom, pos])
    previous_start = 0
    previous_end = 0
    previous_caller = ""
    previous_filter = ""
    previous_varid = ""
    overlaps = []
    overlap_id = []
    for idx, row in all_indels_sorted.iterrows():
        if row['overlap'] == "ABSOLUTE":  # if already consensus then there is absolute overlap
            overlaps += [row['overlap']]
            overlap_id += [row['overlap_id']]
        else:
            current_start = row[pos] - window
            current_end = row[pos] + (len(row[alt]) - 1) + window
            current_caller = row["Caller"]
            try:
                current_filter = row["FILTER"]
            except KeyError:  # means there is no filter - we assume pass
                current_filter = "PASS"
            overlap_id, overlaps = seek_overlap(current_caller, current_end, current_filter, current_start, overlap_id,
                                                overlaps, previous_caller, previous_end, previous_filter,
                                                previous_start, previous_varid)
            # save current values as previous
            previous_start = current_start + window
            previous_end = current_end - window
            previous_caller = current_caller[:]
            previous_filter = current_filter[:]
            previous_varid = row['DNAchange']
    all_indels_sorted['overlap'] = overlaps
    all_indels_sorted['overlap_id'] = overlap_id
    # back into a list
    indels_list = []
    for caller in caller_names:  # to keep order
        indels_list += [all_indels_sorted[all_indels_sorted['Caller'] == caller]]
    return indels_list


def seek_overlap(current_caller, current_end, current_filter, current_start, overlap_id, overlaps, previous_caller,
                 previous_end, previous_filter, previous_start, previous_varid):
    if current_start <= previous_start and current_end >= previous_start:
        if previous_end <= current_end:
            #  |-------|
            #     |--|
            overlaps += ["TOTAL"]
        else:
            # |-------|
            #     |---------|
            overlaps += ["PARTIAL_RIGHT"]
        overlap_id += [previous_varid]
    elif current_start >= previous_start and current_start <= previous_end:
        if current_end >= previous_end:
            #   |-------|
            # |-----|
            overlaps += ["PARTIAL_LEFT"]
        else:
            #   |----|
            #  |-------|
            overlaps += ["TOTAL"]
        overlap_id += [previous_varid]
    else:
        overlaps += ["NONE"]
        overlap_id += ['.']
    if current_caller == previous_caller:
        overlaps[-1] = "NONE"  # this is not really an overlap but a multiallelic variant
        overlap_id[-1] = '.'
    # if overlap but both filters fail then they overlap but we assign a LOWQ tag
    elif current_filter != "PASS" and previous_filter != "PASS" and overlaps[-1] != "NONE":
        overlaps[-1] = overlaps[-1] + "_LOWQ"
    return overlap_id, overlaps


def add_consensus_info_to_meta(meta):
    new_info = [
        '##INFO=<ID=overlap_type,Number=1,Type=String,Description="Overlap type for an indel: ABSOLUTE (exact match), TOTAL (one variant completely overlaps), PARTIAL_LEFT/RIGHT.">',
        '##INFO=<ID=consensus_count,Number=1,Type=Integer,Description="Number of callers supporting this variant">',
        '##INFO=<ID=overlap_id,Number=1,Type=String,Description="Overlap ID">',
        '##INFO=<ID=consensus_callers,Number=1,Type=String,Description="Callers that detected the variant">'
    ]
    meta += '\n'.join(new_info) + '\n'
    return meta


def add_consensus_info_to_info(variants, variants_in_consensus):
    # add new info to INFO field
    for idx, row in variants.iterrows():
        if row["overlap"] == "":
            row["overlap"] = "."
        if row["overlap_id"] == "":
            row["overlap_id"] = "."
        info = row["INFO"]
        info += ";overlap_type=" + row["overlap"]
        info += ";consensus_count=" + str(row["consensus_count"])
        info += ";overlap_id=" + row["overlap_id"]
        for caller, consensus_variants in variants_in_consensus.items():
            if row["DNAchange"] in consensus_variants:
                if 'consensus_callers' not in info:
                    info += f";consensus_callers={caller}"
                else:
                    info += f",{caller}"
        if info.startswith(";"):
            info = info[1:]
        variants.at[idx, 'INFO'] = info
    return variants


def add_consensus_info(calls_dict, callers_name, variants_in_consensus, chrom, pos, maf):
    # put together info for one caller
    caller_dict = {x: {'meta': '', 'variants': []} for x in callers_name}
    for idx, caller in enumerate(callers_name):
        meta = calls_dict['meta'][idx]  # header
        meta = add_consensus_info_to_meta(meta)
        caller_set = pd.concat([calls_dict["SNVs"][idx],
                                calls_dict["MNPs"][idx],
                                calls_dict["INDELs"][idx],
                                ]).sort_values([chrom, pos]).reset_index()
        caller = caller_set["Caller"].unique()[0]  # order can get mess up with python dict
        if maf:
            caller_set["INFO"] = ""
        caller_set = add_consensus_info_to_info(caller_set, variants_in_consensus)
        caller_dict[caller]['meta'] = meta
        caller_dict[caller]['variants'] = caller_set
    return caller_dict


def write_vcf_and_intervals(caller_dict, caller_names, vcf_list, prefix, headers, chrom, pos, maf):
    all_calls = []
    all_calls_txt = []
    for caller, vcf, header in zip(caller_names, vcf_list, headers):
        vcf_out = f"{prefix}_{caller}.vcf"
        txt_out = f"{prefix}_{caller}.txt"  # a txt file with the consensus info
        with open(vcf_out, "w") as out:
            meta = caller_dict[caller]['meta']
            meta_contigs = [x for x in meta.split("\n") if "##contig" in x]
            out.write(meta)
        variants = caller_dict[caller]['variants']
        variants["ID"] = variants['DNAchange']
        if "FILTER" not in variants.columns:
            variants["FILTER"] = "."
        to_txt = variants[["DNAchange", "FILTER", "Caller", "overlap",
                           "overlap_id", "consensus_count"]]

        to_txt = to_txt.assign(consensus_callers=variants["INFO"].str.extract("consensus_callers=(\S+)"))
        to_txt.to_csv(txt_out, mode="w", index=False, header=True, sep="\t")
        if not maf:
            variants[header].to_csv(vcf_out, mode="a", index=False, header=True, sep="\t")
        all_calls += [variants]
        all_calls_txt += [to_txt]
    all_out = f"{prefix}.vcf"
    # consensus txt
    all_calls_txt = pd.concat(all_calls_txt).reset_index()
    all_calls_txt = all_calls_txt[all_calls_txt.consensus_callers.str.contains(",")]
    all_calls_txt.drop(["Caller"], axis=1, inplace=True)
    all_calls_txt = all_calls_txt.drop_duplicates("DNAchange")
    all_calls_txt.sort_values("DNAchange", inplace=True)
    all_calls_txt.to_csv(f"{prefix}_consensus.txt", mode="w", index=False, header=True, sep="\t")
    # consensus vcf
    if not maf:
        all_calls = pd.concat(all_calls).reset_index()
        all_calls[pos] = all_calls[pos].astype(int)
        all_calls.sort_values([chrom, pos], inplace=True)
        # write a basic consensus VCF
        consensus_variants = all_calls[all_calls['overlap'] != "NONE"]
        # Generate a minimal VCF for forcing variant calling
        consensus_variants = consensus_variants[
            ["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT"]]
        consensus_variants['QUAL'] = "."
        consensus_variants['FILTER'] = "."
        consensus_variants['INFO'] = "."
        consensus_variants['FORMAT'] = "."
        consensus_variants['SAMPLE'] = "."
        consensus_variants = consensus_variants.drop_duplicates("ID")
        meta = f"##fileformat=VCFv4.2\n##source=Consensus{len(caller_names)}Callers\n" + "\n".join(meta_contigs) + "\n"
        with open(all_out, "w") as int_out:
            int_out.write(meta)
        consensus_variants.to_csv(all_out, mode="a", index=False, header=True, sep="\t")


def get_calls_from_callers(vcfs, caller_names, chrom, pos, ref, alt):
    # get calls from each caller
    original_vcf_headers = []
    calls_dict = {"SNVs": [], "MNPs": [], "INDELs": [], "meta": []}
    variants_in_consensus = {}
    for caller, vcf in zip(caller_names, vcfs):
        meta, header, df = vcf_to_pandas(vcf)
        print(f"- {caller} has {len(df.index)}")
        original_vcf_headers += [header]
        calls = add_varid_and_type(df=df, caller=caller, chrom=chrom, pos=pos, ref=ref, alt=alt)
        calls_dict["SNVs"] += [(calls["SNVs"])]
        calls_dict["MNPs"] += [calls["MNPs"]]
        calls_dict["INDELs"] += [calls["INDELs"]]
        calls_dict["meta"] += [meta]
        variants_in_consensus[caller] = calls["SNVs"]['DNAchange'].tolist() + \
                                        calls["INDELs"]['DNAchange'].tolist() + \
                                        calls["MNPs"]['DNAchange'].tolist()
    return original_vcf_headers, calls_dict, variants_in_consensus


def do_consensus(indel_window, calls_dict, caller_names, chrom, pos, alt):
    # perform consensus
    consensus = {}
    for var_type in calls_dict.keys():
        if var_type != "meta":  # meta is for the VCF headers only
            consensus[var_type] = consensus_exact_match(calls_dict[var_type])
        else:
            consensus['meta'] = calls_dict['meta']
    # extra consensus check for indels tha might overlap
    consensus["INDELs"] = consensus_overlap(list_of_calls=consensus["INDELs"], caller_names=caller_names,
                                            window=indel_window, chrom=chrom, pos=pos, alt=alt)
    return consensus


def main():
    args = argparser()
    if args.maf:
        chrom = "Chromosome"
        pos = "Start_Position"  # can be sometimes Start_position (no capital p)
        ref = "Reference_Allele"
        alt = "Tumor_Seq_Allele2"
    else:
        chrom = "#CHROM"
        pos = "POS"
        ref = "REF"
        alt = "ALT"
    # get callers names
    caller_names = assign_callers_names(caller_names=args.names, vcfs=args.input)
    # get calls
    original_vcf_headers, calls_dict, variants_in_consensus = get_calls_from_callers(vcfs=args.input,
                                                                                     caller_names=args.names,
                                                                                     chrom=chrom, pos=pos, ref=ref,
                                                                                     alt=alt)
    # consensus
    consensus = do_consensus(indel_window=args.indel_window,
                             calls_dict=calls_dict,
                             caller_names=caller_names,
                             chrom=chrom, pos=pos, alt=alt)
    # add info to output
    caller_dict = add_consensus_info(calls_dict=consensus, callers_name=caller_names,
                                     variants_in_consensus=variants_in_consensus,
                                     chrom=chrom, pos=pos, maf=args.maf)
    # write to separate vcfs
    write_vcf_and_intervals(caller_dict=caller_dict, caller_names=caller_names, vcf_list=args.input, prefix=args.prefix,
                            headers=original_vcf_headers, chrom=chrom, pos=pos, maf=args.maf)


if __name__ == '__main__':
    main()
