include { ALIGNMENT_TO_FASTQ as BAM_TO_FASTQ  } from '../nf-core/alignment_to_fastq'
include { RUN_FASTQC                          } from '../nf-core/run_fastqc'
include { FASTP                               } from '../../modules/nf-core/modules/fastp/main'
include { GATK4_MAPPING                       } from '../nf-core/gatk4/mapping/main'
include { ALIGN_STAR                          } from '../nf-core/align_star'
include { MERGE_INDEX_BAM                     } from '../nf-core/merge_index_bam'
include { MAPPING_CSV                         } from '../local/mapping_csv'



workflow MAPPING {

    take:
        bwa
        bwamem2
        dragmap
        star_index
        gtf
        ch_input_sample

    main:
        ch_reports   = Channel.empty()
        ch_versions  = Channel.empty()

        // Gather index for mapping given the chosen aligner
        ch_map_index = params.aligner == "bwa-mem" ? bwa : params.aligner == "bwa-mem2" ? bwamem2 : dragmap

        if (params.step in ['mapping']) {
            // Separate input in bam or fastq
            ch_input_sample.branch{
                bam:   it[0].data_type == "bam"
                fastq: it[0].data_type == "fastq"
            }.set{ch_input_sample_type}

            // STEP 1.A: convert any bam input to fastq
            BAM_TO_FASTQ(ch_input_sample_type.bam, [])
            ch_versions = ch_versions.mix(BAM_TO_FASTQ.out.versions)
            // gather fastq (from input or converted with BAM_TO_FASTQ)
            // Theorically this could work on mixed input (fastq for one sample and bam for another)
            ch_input_fastq = ch_input_sample_type.fastq.mix(BAM_TO_FASTQ.out.reads)


            // STEP 1.B: QC
            if (!(params.skip_tools && params.skip_tools.split(',').contains('fastqc'))) {
                RUN_FASTQC(ch_input_fastq)
                ch_reports  = ch_reports.mix(RUN_FASTQC.out.fastqc_zip.collect{meta, logs -> logs})
                ch_versions = ch_versions.mix(RUN_FASTQC.out.versions)
            }


            //  STEP 1.C: Trimming and/or splitting
            if (params.trim_fastq || params.split_fastq > 0) {
                // Call FASTP for trimming
                FASTP(ch_input_fastq, params.save_trimmed_fail, params.save_merged_fastq)
                ch_reports = ch_reports.mix(
                                        FASTP.out.json.collect{meta, json -> json},
                                        FASTP.out.html.collect{meta, html -> html}
                                        )
                // Map channel by split group
                if(params.split_fastq){
                    ch_reads_to_map = FASTP.out.reads.map{ key, reads ->
                            read_files = reads.collate(2)  // removed sorting because gace concurrent error - it looks like files are sorted already
                            [[
                                data_type:key.data_type,
                                id:key.id,
                                numLanes:key.numLanes,
                                patient: key.patient,
                                read_group:key.read_group,
                                sample:key.sample,
                                size:read_files.size(),
                                status:key.status,
                            ],
                            read_files]
                        }.transpose()
                } else {
                    ch_reads_to_map = FASTP.out.reads
                }

                ch_versions = ch_versions.mix(FASTP.out.versions)
            } else {
                ch_reads_to_map = ch_input_fastq
            }


            //  STEP 1.D: MAPPING READS TO REFERENCE GENOME
            // Generate mapped reads channel for alignment
            ch_reads_to_map = ch_reads_to_map.map{ meta, reads ->
                // update ID when no multiple lanes or splitted fastqs
                new_id = meta.size * meta.numLanes == 1 ? meta.sample : meta.id

                [[
                    data_type:  meta.data_type,
                    id:         new_id,
                    numLanes:   meta.numLanes,
                    patient:    meta.patient,
                    read_group: meta.read_group,
                    sample:     meta.sample,
                    size:       meta.size,
                    status:     meta.status,
                    ],
                reads]
            }
            // Separate DNA from RNA samples, DNA samples will be aligned with bwa, and RNA samples with star
            ch_reads_to_map.branch{
                dna: it[0].status < 2
                rna: it[0].status == 2
            }.set{ch_reads_to_map_status}

            //  STEP 1.D.1: DNA mapping with BWA
            sort_bam = true // TODO: set up as parameter
            GATK4_MAPPING(ch_reads_to_map_status.dna, ch_map_index, sort_bam)
            ch_versions = ch_versions.mix(GATK4_MAPPING.out.versions)
            // Grouping the bams from the same samples
            bwa_bams = GATK4_MAPPING.out.bam
            ch_bam_mapped_dna = GATK4_MAPPING.out.bam.map{ meta, bam ->
                numLanes = meta.numLanes ?: 1
                size     = meta.size     ?: 1
                // remove no longer necessary fields:
                //   read_group: Now in the BAM header
                //     numLanes: Was only needed for mapping
                //         size: Was only needed for mapping
                lane = ( meta.read_group =~ /PU:(\S+?)(\\t|\s|\"|$)/ )[0][1]

                new_meta = [
                            id:meta.sample,        // update ID to be based on the sample name
                            data_type:"bam",       // update data_type
                            patient:meta.patient,
                            sample:meta.sample,
                            status:meta.status,
                            lane:lane
                        ]
                // Use groupKey to make sure that the correct group can advance as soon as it is complete
                [ groupKey(new_meta, numLanes * size), bam]
            }.groupTuple()


            // RNA will be aligned with STAR
    //        ch_reads_to_map_rna = ch_reads_to_map_status.rna.map{ meta, reads -> [meta, reads] }
            // STAR
            ALIGN_STAR (
                ch_reads_to_map_status.rna,
                star_index,
                gtf,
                params.star_ignore_sjdbgtf,
                params.seq_platform ? params.seq_platform : [],
                params.seq_center ? params.seq_center : []
            )
            // Grouping the bams from the same samples not to stall the workflow
            star_bams = ALIGN_STAR.out.bam.groupTuple()
            ch_bam_mapped_rna = ALIGN_STAR.out.bam.map{ meta, bam ->
                numLanes = meta.numLanes ?: 1
                size     = meta.size     ?: 1
                // remove no longer necessary fields:
                //   read_group: Now in the BAM header
                //     numLanes: Was only needed for mapping
                //         size: Was only needed for mapping
                lane = ( meta.read_group =~ /PU:(\S+?)(\\t|\s|\"|$)/ )[0][1]
                new_meta = [
                            id:meta.sample, // update ID to be based on the sample name
                            data_type:"bam", // update data_type
                            patient:meta.patient,
                            sample:meta.sample,
                            status:meta.status,
                            lane:lane
                        ]
                // Use groupKey to make sure that the correct group can advance as soon as it is complete
                [ groupKey(new_meta, numLanes * size), bam]
            }.groupTuple()
            // Gather QC reports
            ch_reports           = ch_reports.mix(ALIGN_STAR.out.stats.collect{it[1]}.ifEmpty([]))
            ch_reports           = ch_reports.mix(ALIGN_STAR.out.log_final.collect{it[1]}.ifEmpty([]))
            ch_versions          = ch_versions.mix(ALIGN_STAR.out.versions)

            // mix dna and rna in one channel
            ch_bam_mapped = ch_bam_mapped_dna.mix(ch_bam_mapped_rna)
            // gatk4 markduplicates can handle multiple bams as input, so no need to merge/index here
            // Except if and only if skipping markduplicates or saving mapped bams
            if (params.save_bam_mapped || (params.skip_tools && params.skip_tools.split(',').contains('markduplicates'))) {

                // bams are merged (when multiple lanes from the same sample), indexed and then converted to cram
                MERGE_INDEX_BAM(ch_bam_mapped)
                // Create CSV to restart from this step
                MAPPING_CSV(MERGE_INDEX_BAM.out.bam_bai.transpose())

                // Gather used softwares versions
                ch_versions = ch_versions.mix(MERGE_INDEX_BAM.out.versions)
            }
        }
        else {
            ch_input_sample.branch{
                bam:   it[0].data_type == "bam"
                fastq: it[0].data_type == "fastq"
            }.set{ch_input_sample_type}
            ch_bam_mapped = ch_input_sample_type.bam

            ch_bam_mapped.branch{
            rna:    it[0].status >= 2
            dna:    it[0].status < 2
            }.set{ch_input_sample_class}
            star_bams = ch_input_sample_class.rna
            bwa_bams = ch_input_sample_class.dna
        }


    emit:
        star_bams      = star_bams      //second pass with RG tags
        bwa_bams       = bwa_bams       // second pass with RG tags
        ch_bam_mapped  = ch_bam_mapped  // for preprocessing
        reports        = ch_reports
        versions       = ch_versions

}